import { Panel } from "../components/common/Panel.jsx";
import { DataTable } from "../components/common/DataTable.jsx";
import { Bars } from "../components/common/Bars.jsx";
import { Coverage } from "../components/coverage/Coverage.jsx";
import { CoverageBars } from "../components/coverage/CoverageBars.jsx";
import { useQuery } from "../hooks/useQuery.js";
import { TYPES } from "../config/capture.js";
import { useState } from "react";
import { analysisRefreshInterval } from "../utils/polling.js";

export function Analysis({ snapshot }) {
  const [revision, setRevision] = useState(0);
  const refreshInterval = analysisRefreshInterval(snapshot);
  const {
    data: a,
    error,
    loading,
  } = useQuery(`/runs/${snapshot.run.id}/analysis`, refreshInterval, revision);
  if (error)
    return (
      <p className="error-banner" role="alert">
        {error}
      </p>
    );
  if (!a)
    return (
      <p className="table-empty">
        {loading
          ? "Calculating analysis from saved evidence…"
          : "No analysis available."}
      </p>
    );
  const typeRows = Object.entries(snapshot.types)
    .filter(([, value]) => value.discovered)
    .map(([kind, value]) => ({
      Type: TYPES[kind],
      Eligible: value.eligible,
      Found: value.found,
      Missing: value.missing,
      Unverified: value.unverified,
      "Not tracked by corpus": value.not_tracked || 0,
      "Verified coverage %": value.verified_coverage,
      "Verification %": value.verification_completion,
    }));
  return (
    <>
      <p className="scoped-summary">
        {refreshInterval
          ? "Live saved evidence · updates automatically every 15 seconds during this scan."
          : "Saved evidence · analysis refreshes on entry or request."}
        <button
          className="secondary"
          onClick={() => setRevision((value) => value + 1)}
        >
          Refresh analysis
        </button>
      </p>
      <div className="overview-grid">
        <Coverage snapshot={snapshot} />
        <Panel
          title="Coverage by captured type"
          subtitle="URL presence only · N/A means no confirmed result"
        >
          <CoverageBars
            rows={typeRows.map((row) => ({
              ...row,
              "Coverage %": row["Verified coverage %"],
            }))}
            label="Type"
          />
          <DataTable rows={typeRows} />
        </Panel>
      </div>
      <Panel
        title="Coverage by area"
        subtitle="Unique URLs in each area · areas may overlap"
      >
        <DataTable
          rows={(a.segments || []).map((row) =>
            Object.fromEntries(
              [
                "Area",
                "Valid URLs",
                "Found",
                "Missing",
                "Unverified",
                "Coverage %",
              ].map((key) => [key, row[key]]),
            ),
          )}
        />
      </Panel>
      {[
        ["Pagination", "Pagination coverage and missing ranges", a.pagination],
        ["Filters", "Filter coverage", a.filter_states],
        ["Archives", "Archive coverage by year", a.years],
      ].map(([area, title, rows]) => {
        const assessment = a.discovery_status?.find(
          (item) => item.Area === area,
        );
        return (
          <details className="analysis-disclosure" key={area}>
            <summary>
              {title}
              <span>
                {a.segments?.find((item) => item.Area === area)?.[
                  "Valid URLs"
                ] || 0}{" "}
                URLs
              </span>
            </summary>
            <p className="footnote">
              {assessment?.Evidence ||
                "No comparable URL evidence was recorded for this view; coverage cannot be inferred from an empty table."}
            </p>
            <DataTable
              rows={rows || []}
              empty="N/A - no measurable URL states recorded in this inventory."
            />
          </details>
        );
      })}
      <div className="overview-grid">
        <Panel
          title="First discovery source"
          subtitle="Disjoint first-source counts; historical first sources may be unknown"
        >
          <Bars items={a.sources} label="source" />
        </Panel>
        <Panel
          title="Website validation outcomes"
          subtitle="File URL collection is not file availability validation"
        >
          <Bars items={a.http_outcomes} label="outcome" />
        </Panel>
      </div>
      {!!a.depth.length && (
        <Panel
          title="HTML navigation depth"
          subtitle="Actual link distance · sitemap membership alone has unknown navigation depth"
        >
          <CoverageBars rows={a.depth} label="Depth" />
          <DataTable rows={a.depth} />
        </Panel>
      )}
      {!!snapshot.hosts.length && (
        <Panel
          title="Captured host distribution"
          subtitle="Target and linked resource hosts, not separately tested websites"
        >
          <Bars items={snapshot.hosts} label="host" />
          <DataTable rows={snapshot.hosts} />
        </Panel>
      )}
      {[
        [
          "Discovery source progress",
          a.discovery_reconciliation?.sources || [],
          "Source tasks, not page counts. Failed or pending sources leave discovery unresolved.",
        ],
        [
          "Public collection reconciliation",
          a.discovery_reconciliation?.collections || [],
          "Published totals apply to that API collection and observation time, not the entire website.",
        ],
        [
          "Pagination probe evidence",
          a.pagination_probes,
          "Up to 200 recorded probes; blocking or non-monotonic content does not prove a maximum.",
        ],
        [
          "Coverage by website section",
          a.sections ? a.sections_table || [] : [],
          "",
        ],
        [
          "Resource subtypes",
          a.subtypes,
          "MIME evidence takes priority where available. Files remain URL-only.",
        ],
        [
          "Sitemap contribution",
          a.sections.sitemaps,
          `${a.section_totals.sitemaps} records; table shows up to 200. A sitemap is not a complete website inventory.`,
        ],
        [
          "Browser / API requests",
          a.sections.network,
          `${a.section_totals.network} records; table shows up to 200. These are observed requests, not all possible APIs.`,
        ],
        [
          "Also discovered through",
          a.also_sources,
          "Non-additive source counts: a URL may appear under multiple methods.",
        ],
        [
          "Repeated HTML fingerprints",
          a.html_duplicates,
          "Possible aliases/shared content, not proof of duplicate pages.",
        ],
        [
          "Comparable audit history",
          a.history,
          "Same target, captured types, discovery/scope settings and Corpus provider; up to 12 runs.",
        ],
      ]
        .filter(([, rows]) => rows?.length)
        .map(([title, rows, note]) => (
          <details className="analysis-disclosure" key={title}>
            <summary>
              {title}
              <span>{rows.length} rows</span>
            </summary>
            <p className="footnote">{note}</p>
            <DataTable rows={rows} />
          </details>
        ))}
      {!!a.gaps.length && (
        <Panel
          title="Patterns worth investigating"
          subtitle="Likely causes require production crawler logs, not assumptions"
        >
          {a.gaps.map((gap, index) => (
            <div className="gap" key={index}>
              <div>
                <strong>{gap.kind.replaceAll("_", " ")}</strong>
                <p>{gap.evidence}</p>
              </div>
            </div>
          ))}
        </Panel>
      )}
      <Panel
        title="HTML text comparison"
        subtitle="Saved website text compared with stored Corpus HTML text"
      >
        <DataTable
          rows={Object.entries(a.content_counts || {}).map(
            ([status, count]) => ({
              "Text comparison": status,
              "HTML pages": count,
            }),
          )}
        />
      </Panel>
      <p className="info-box">
        HTML word coverage is estimated, not exact semantic completeness.
        Capture dates can differ; unavailable or truncated stored text is not
        scored. Files remain URL-only. CrawlScope cannot prove discovery of
        disconnected, private or blocked content.
      </p>
      <div className="report-links">
        <a
          className="secondary"
          href={`/api/runs/${snapshot.run.id}/reports/html`}
          target="_blank"
          rel="noreferrer"
        >
          Offline HTML report
        </a>
        <a
          className="secondary"
          href={`/api/runs/${snapshot.run.id}/reports/xlsx`}
        >
          Excel report
        </a>
      </div>
    </>
  );
}
