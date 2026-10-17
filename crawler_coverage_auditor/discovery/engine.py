"""Additive adapters scheduled by the existing serial audit controller."""

import json
import os
import re
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from crawler_coverage_auditor.services.discovery_tasks import DiscoveryTasks
from crawler_coverage_auditor.validation.normalize import normalize_url, same_scope

from .advanced import (
    inline_scripts,
    manifest_links,
    metadata_links,
    public_candidate,
    source_tasks,
    wordpress_collections,
)
from .javascript import JavaScriptMiner
from .static_links import Link

SOURCE = {
    "SCRIPT": "JS_SOURCE",
    "MANIFEST": "METADATA",
    "WORDPRESS_ROOT": "PUBLIC_API",
    "WORDPRESS_LIST": "PUBLIC_API",
    "RESOURCESYNC": "RESOURCESYNC",
    "WAYBACK": "EXTERNAL_INDEX",
    "COMMONCRAWL": "EXTERNAL_INDEX",
}


class AdvancedDiscovery:
    def __init__(self, auditor):
        self.audit = auditor
        self.tasks = DiscoveryTasks(auditor.db, auditor.run_id)
        self.javascript = JavaScriptMiner()

    def enqueue(self, kind, url, parent, depth):
        if not self.audit.config.discovers(SOURCE[kind]):
            return False
        url = public_candidate(url, parent or self.audit.config.target_url)
        if not url:
            return False
        if kind not in {"SCRIPT", "WAYBACK", "COMMONCRAWL"} and not same_scope(
            url, self.audit.config.seed_hosts, self.audit.config.include_subdomains
        ):
            return False
        return self.tasks.enqueue(kind, normalize_url(url), parent, depth)

    def prepare_upgrade(self, *, revisit_sources=True):
        db, run_id = self.audit.db, self.audit.run_id
        candidates = [
            dict(row)
            for row in db.connection.execute(
                """SELECT u.normalized_url,u.state,
            COALESCE(p.browser_completed,0) AS browser_completed FROM urls u JOIN http_results h
            ON h.audit_run_id=u.audit_run_id AND h.normalized_url=u.normalized_url
            LEFT JOIN url_checkpoints p ON p.audit_run_id=u.audit_run_id AND p.normalized_url=u.normalized_url
            WHERE u.audit_run_id=? AND u.page_type='HTML' AND u.state='DONE' AND
            (h.validity='VALID_PAGE' OR h.validity IN ('DNS_FAILURE','FETCH_ERROR','TIMEOUT','CONNECTION_FAILURE'))""",
                (run_id,),
            )
        ]
        if not revisit_sources:
            # Wayback-only upgrades add a durable source task; saved website and
            # Corpus results need not be crawled/checked all over again.
            candidates = []
        for row in candidates:
            db.connection.execute(
                "UPDATE urls SET state='PENDING' WHERE audit_run_id=? AND normalized_url=?",
                (run_id, row["normalized_url"]),
            )
            db.connection.execute(
                """INSERT INTO url_checkpoints VALUES(?,?,0,?,0,datetime('now'))
                ON CONFLICT(audit_run_id,normalized_url) DO UPDATE SET website_completed=0,browser_completed=0,completed=0""",
                (run_id, row["normalized_url"], 0),
            )
        db.connection.execute(
            "UPDATE audit_runs SET discovery_completed_at=NULL,completed_at=NULL,discovery_engine_version=?,status='RUNNING' WHERE id=?",
            (self.audit.config.discovery_version, run_id),
        )
        db.connection.commit()
        return len(candidates)

    def seed_external(self):
        if not self.audit.config.discovers("EXTERNAL_INDEX"):
            return
        host = urlsplit(self.audit.config.target_url).hostname
        query = urlencode(
            {
                "url": host + "/*",
                "output": "json",
                "fl": "original,timestamp",
                "collapse": "urlkey",
                "showResumeKey": "true",
                "limit": "10000",
            }
        )
        self.enqueue(
            "WAYBACK",
            "https://web.archive.org/cdx/search/cdx?" + query,
            self.audit.config.target_url,
            0,
        )
        index = os.getenv("CRAWLSCOPE_COMMONCRAWL_INDEX", "")
        if index:
            if not re.fullmatch(r"CC-MAIN-\d{4}-\d{2}", index):
                self.audit._limit(
                    "Historical indexes",
                    self.audit.config.target_url,
                    "Common Crawl index setting has an invalid collection ID",
                )
                return
            query = urlencode(
                {
                    "url": host + "/*",
                    "output": "json",
                    "showNumPages": "true",
                    "pageSize": "1",
                }
            )
            self.enqueue(
                "COMMONCRAWL",
                f"https://index.commoncrawl.org/{index}-index?" + query,
                self.audit.config.target_url,
                0,
            )

    async def inspect(self, html, url, depth, headers=None):
        if self.audit.config.discovers("METADATA"):
            self.audit._collect_links(
                metadata_links(html, url, headers),
                "METADATA",
                depth + 1,
                normalize_url(url),
            )
        for kind, target in source_tasks(html, url, headers):
            self.enqueue(kind, target, url, depth)
        if self.audit.config.discovers("JS_SOURCE"):
            for script in inline_scripts(html):
                if self.audit.stopped():
                    return
                digest = sha256(script.encode()).hexdigest()
                task = {
                    "kind": "INLINE_SCRIPT",
                    "url": "inline:" + digest,
                    "version": self.tasks.VERSION,
                }
                parsed = self.tasks.cached(task)
                if parsed is None:
                    parsed = await self.javascript.parse(script)
                    parsed = self._sanitize_script(parsed, url)
                    if not parsed.get("error"):
                        self.tasks.cache(task, digest, parsed)
                self._script_links(parsed, url, depth)
                for raw in parsed.get("imports", []):
                    self.enqueue("SCRIPT", urljoin(url, raw), url, depth)

    def _sanitize_script(self, parsed, parent):
        return dict(
            parsed,
            urls=[
                raw for raw in parsed.get("urls", []) if public_candidate(raw, parent)
            ],
            patterns=[
                raw
                for raw in parsed.get("patterns", [])
                if public_candidate(raw, parent)
            ],
            imports=[
                raw
                for raw in parsed.get("imports", [])
                if public_candidate(raw, parent)
            ],
        )

    def _script_links(self, parsed, parent, depth):
        if parsed.get("error"):
            self.audit._limit("JavaScript source", parent, parsed["error"])
        links = [
            Link(url, "Concrete JavaScript URL candidate", "script")
            for raw in parsed.get("urls", [])
            if (url := public_candidate(raw, parent))
        ]
        self.audit._collect_links(links, "JS_SOURCE", depth + 1, normalize_url(parent))
        if parsed.get("patterns"):
            self.audit.db.content_evidence(
                self.audit.run_id,
                normalize_url(parent),
                {
                    "route_patterns": parsed["patterns"],
                    "route_patterns_note": "Templates are not counted as existing pages; concrete values are required.",
                },
            )

    async def step(self, client):
        task = self.tasks.pending()
        if not task or self.audit.stopped():
            return False
        kind, url = task["kind"], task["url"]
        # Respect disabled adapters after resuming with changed settings.
        if not self.audit.config.discovers(SOURCE[kind]):
            self.tasks.save(
                task, "SKIPPED", evidence={"reason": "Discovery source disabled"}
            )
            return True
        self.tasks.save(task, "RUNNING", cursor=json.loads(task["cursor_json"]))
        self.audit._phase("Discovering published URL sources")
        self.audit._event(
            "SOURCE_CHECKING",
            'Querying Wayback for historical URL candidates; live validation follows' if kind == 'WAYBACK' else f"Inspecting published {kind.lower().replace('_', ' ')} discovery source",
            url,
        )
        cached = self.tasks.cached(task)
        cursor = json.loads(task["cursor_json"])
        try:
            if cached is None:
                parts = urlsplit(url)
                request_url = urlunsplit(
                    (
                        *parts[:3],
                        urlencode(
                            [
                                (key, value)
                                for key, value in parse_qsl(parts.query)
                                if key != "crawlscope_pages"
                            ]
                        ),
                        "",
                    )
                )
                response = await client.get(
                    request_url, discovery_metadata=True, referer=task["parent_url"]
                )
                if self.audit.stopped():
                    self.tasks.save(task, "PENDING", cursor=cursor)
                    return True
                if response.error or response.url_only or response.status_code != 200:
                    raise ValueError(
                        f"Discovery unavailable: HTTP {response.status_code}; {response.failure_reason or response.error or 'unexpected response'}"
                    )
                cached = await self._parse(task, response)
                self.tasks.cache(task, sha256(response.content).hexdigest(), cached)
            with self.audit.db.discovery_batch():
                for position, item in enumerate(cached.get("links", [])):
                    if position < cursor.get("position", 0):
                        continue
                    if self.audit.stopped():
                        self.tasks.save(
                            task,
                            "PENDING",
                            cursor={"position": position},
                            evidence=cached.get("evidence"),
                        )
                        return True
                    target = public_candidate(item, task["parent_url"] or url)
                    if target:
                        self.audit._add_discovery(
                            target,
                            SOURCE[kind],
                            task["depth"] + 1,
                            task["parent_url"] or url,
                            "Published discovery source",
                        )
                    self.tasks.save(task, "RUNNING", cursor={"position": position + 1})
                for next_kind, next_url in cached.get("tasks", []):
                    self.enqueue(
                        next_kind, next_url, task["parent_url"] or url, task["depth"]
                    )
                self.tasks.save(task, "DONE", evidence=cached.get("evidence") or {})
            resolved = list(
                self.audit.db.connection.execute(
                    "SELECT id,stage,url,reason FROM audit_limits WHERE audit_run_id=? AND stage='Discovery sources' AND url=?",
                    (self.audit.run_id, url),
                )
            )
            for boundary in resolved:
                self.audit.db.connection.execute(
                    "UPDATE audit_limits SET stage='Historical discovery retry' WHERE id=?",
                    (boundary["id"],),
                )
                self.audit._limited.discard(
                    (boundary["stage"], boundary["url"], boundary["reason"])
                )
            self.audit.db.connection.commit()
            self.audit._event(
                "SOURCE_COMPLETED",
                f"{kind}: {len(cached.get('links', [])):,} URL candidates recorded; live validation remains required",
                url,
            )
        except (ValueError, TypeError, KeyError, OSError) as error:
            self.tasks.save(
                task, "FAILED", cursor=cursor, evidence={"reason": str(error)}
            )
            self.audit._limit("Discovery sources", url, str(error))
        return True

    async def _parse(self, task, response):
        kind, parent = task["kind"], task["parent_url"] or task["url"]
        result = {"links": [], "tasks": [], "evidence": {}}
        if kind == "SCRIPT":
            if "javascript" not in response.headers.get(
                "content-type", ""
            ) and not urlsplit(response.final_url).path.endswith((".js", ".mjs")):
                raise ValueError("Referenced script did not return JavaScript")
            parsed = await self.javascript.parse(response.text)
            parsed = self._sanitize_script(parsed, parent)
            if parsed.get("error"):
                raise ValueError(parsed["error"])
            result["links"] = [
                url for raw in parsed["urls"] if (url := public_candidate(raw, parent))
            ]
            result["evidence"]["route_patterns"] = parsed["patterns"]
            result["tasks"].extend(
                ("SCRIPT", urljoin(response.final_url, raw))
                for raw in parsed.get("imports", [])
            )
            if parsed["patterns"]:
                self.audit.db.content_evidence(
                    self.audit.run_id,
                    normalize_url(parent),
                    {"route_patterns": parsed["patterns"]},
                )
        elif kind == "MANIFEST":
            result["links"] = [
                link.url
                for link in manifest_links(
                    json.loads(response.text), response.final_url
                )
            ]
        elif kind == "WORDPRESS_ROOT":
            result["tasks"] = [
                ("WORDPRESS_LIST", url)
                for url in wordpress_collections(
                    json.loads(response.text), response.final_url
                )
            ]
        elif kind == "WORDPRESS_LIST":
            payload = json.loads(response.text)
            if not isinstance(payload, list) or any(
                not isinstance(item, dict) for item in payload
            ):
                raise ValueError("Public content collection is not an item list")
            result["links"] = [
                url
                for item in payload
                if isinstance(item.get("link"), str)
                and (url := public_candidate(item["link"], parent))
            ]
            headers = {name.lower(): value for name, value in response.headers.items()}
            total, pages = headers.get("x-wp-total"), headers.get("x-wp-totalpages")
            parts = urlsplit(task["url"])
            query = dict(parse_qsl(parts.query))
            page = int(query.get("page", "1"))
            result["evidence"] = {
                "reported_total": int(total) if total and total.isdigit() else None,
                "reported_pages": int(pages) if pages and pages.isdigit() else None,
                "page": page,
                "item_ids": [
                    item["id"] for item in payload if isinstance(item.get("id"), int)
                ],
                "note": "Totals apply to this API collection and observation time, not the entire website.",
            }
            if pages and pages.isdigit() and page < int(pages):
                query["page"] = str(page + 1)
                result["tasks"].append(
                    ("WORDPRESS_LIST", urlunsplit((*parts[:3], urlencode(query), "")))
                )
            elif not pages:
                raise ValueError(
                    "Collection returned no pagination boundary metadata; maximum is unverified"
                )
        elif kind == "RESOURCESYNC":
            from xml.etree import ElementTree as ET

            if (
                b"<!DOCTYPE" in response.content.upper()
                or b"<!ENTITY" in response.content.upper()
            ):
                raise ValueError("ResourceSync DTD/entities are not permitted")
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError as error:
                raise ValueError("ResourceSync document is not valid XML") from error
            ns = "{http://www.openarchives.org/rs/terms/}"
            metadata = root.find(ns + "md")
            capability = metadata.get("capability") if metadata is not None else ""
            if capability not in {
                "description",
                "capabilitylist",
                "resourcelist",
                "changelist",
            }:
                raise ValueError(
                    "Unsupported ResourceSync capability; dumps are not downloaded"
                )
            for entry in root:
                loc = next(
                    (
                        child.text
                        for child in entry
                        if child.tag.rsplit("}", 1)[-1] == "loc"
                    ),
                    None,
                )
                if not loc:
                    continue
                md = entry.find(ns + "md")
                child_cap = md.get("capability") if md is not None else None
                deleted = md is not None and md.get("change") == "deleted"
                if capability in {"description", "capabilitylist"} or root.tag.endswith(
                    "sitemapindex"
                ):
                    if child_cap not in {"resourcedump", "changedump"}:
                        result["tasks"].append(("RESOURCESYNC", loc.strip()))
                elif not deleted:
                    result["links"].append(loc.strip())
            result["evidence"] = {
                "capability": capability,
                "from": metadata.get("from"),
                "until": metadata.get("until"),
                "note": "Published collection evidence; change lists alone do not establish a full baseline.",
            }
        elif kind == "WAYBACK":
            payload = json.loads(response.text)
            if not isinstance(payload, list):
                raise ValueError("Archive index returned no readable enumeration")
            if not payload:
                result["evidence"] = {
                    "note": "No URL candidates returned by this archive query; this does not establish that the website has no pages."
                }
                return result
            columns = payload[0]
            if "original" not in columns:
                raise ValueError("Archive index omitted URL field")
            result["links"] = []
            resumes = []
            separator = False
            for row in payload[1:]:
                if row == []:
                    separator = True
                elif (
                    not separator and isinstance(row, list) and len(row) == len(columns)
                ):
                    result["links"].append(row[columns.index("original")])
                elif (
                    separator
                    and isinstance(row, list)
                    and len(row) == 1
                    and isinstance(row[0], str)
                ):
                    resumes.append(row[0])
            if resumes:
                parts = urlsplit(task["url"])
                query = dict(parse_qsl(parts.query))
                if query.get("resumeKey") == resumes[-1]:
                    raise ValueError(
                        "Archive repeated its continuation key; endpoint remains unverified"
                    )
                query["resumeKey"] = resumes[-1]
                result["tasks"].append(
                    ("WAYBACK", urlunsplit((*parts[:3], urlencode(query), "")))
                )
            result["evidence"] = {
                "note": "Historical URL candidates require current validation; archive enumeration is not a complete live inventory."
            }
        elif kind == "COMMONCRAWL":
            parts = urlsplit(task["url"])
            query = dict(parse_qsl(parts.query))
            if query.get("showNumPages") == "true":
                count = json.loads(response.text)
                pages = count.get("pages") if isinstance(count, dict) else None
                if not isinstance(pages, int) or pages < 0:
                    raise ValueError("Index omitted its pagination boundary")
                query.pop("showNumPages")
                query["page"] = "0"
                if pages:
                    query["crawlscope_pages"] = str(pages)
                    result["tasks"].append(
                        ("COMMONCRAWL", urlunsplit((*parts[:3], urlencode(query), "")))
                    )
                result["evidence"]["reported_pages"] = pages
            else:
                result["links"] = [
                    item["url"]
                    for line in response.text.splitlines()
                    if line.strip()
                    and isinstance((item := json.loads(line)), dict)
                    and isinstance(item.get("url"), str)
                ]
                page, pages = (
                    int(query.get("page", "0")),
                    int(query.get("crawlscope_pages", "0")),
                )
                if page + 1 < pages:
                    query["page"] = str(page + 1)
                    result["tasks"].append(
                        ("COMMONCRAWL", urlunsplit((*parts[:3], urlencode(query), "")))
                    )
            result["evidence"]["note"] = (
                "Historical URL candidates from the explicitly selected crawl index; other indexes may contain additional URLs."
            )
        return result

    async def close(self):
        await self.javascript.close()
