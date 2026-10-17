"""Task-local analysis of exact snapshots; never cache a live DOM across pages.

Readers share parsed trees. Destructive consumers explicitly request a copy.
The bounded scope is released after each URL, including failed/cancelled work.
"""

import json
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from time import perf_counter

from bs4 import BeautifulSoup, NavigableString

_CURRENT = ContextVar("crawlscope_page_analysis", default=None)


class PageAnalysis:
    def __init__(self):
        self.trees = OrderedDict()
        self.values = OrderedDict()
        self.parses = self.hits = self.json_parses = self.json_hits = 0
        self.parse_seconds = 0.0

    def soup(self, html, parser="lxml", *, mutable=False):
        key = (html, parser)
        if key not in self.trees:
            started = perf_counter()
            self.trees[key] = BeautifulSoup(html, parser)
            self.parse_seconds += perf_counter() - started
            self.parses += 1
            if len(self.trees) > 2:
                self.trees.popitem(last=False)
        else:
            self.hits += 1
        self.trees.move_to_end(key)
        return deepcopy(self.trees[key]) if mutable else self.trees[key]

    def json(self, content):
        key = ("json", content)
        if key in self.values:
            self.json_hits += 1
            self.values.move_to_end(key)
            return self.values[key]
        self.json_parses += 1
        value = json.loads(content)
        self.values[key] = value
        if len(self.values) > 64:
            self.values.popitem(last=False)
        return value

    def metrics(self):
        return {
            "html_parses": self.parses,
            "html_cache_hits": self.hits,
            "html_parse_seconds": round(self.parse_seconds, 6),
            "json_parses": self.json_parses,
            "json_cache_hits": self.json_hits,
        }


@contextmanager
def page_analysis():
    current = _CURRENT.get()
    if current is not None:
        yield current
        return
    analysis = PageAnalysis()
    token = _CURRENT.set(analysis)
    try:
        yield analysis
    finally:
        _CURRENT.reset(token)


def analyzed(function):
    """Scope async URL/browser work without sharing state between audit tasks."""

    @wraps(function)
    async def wrapped(*args, **kwargs):
        outer = _CURRENT.get() is None
        started = perf_counter()
        with page_analysis() as analysis:
            try:
                return await function(*args, **kwargs)
            finally:
                totals = getattr(args[0], "analysis_metrics", None) if args else None
                if outer and totals is not None:
                    for key, value in analysis.metrics().items():
                        totals[key] = totals.get(key, 0) + value
                    key = function.__name__.lstrip("_") + "_seconds_including_waits"
                    totals[key] = round(
                        totals.get(key, 0) + perf_counter() - started, 6
                    )

    return wrapped


def parsed_html(html, parser="lxml", *, mutable=False):
    current = _CURRENT.get()
    return (
        current.soup(html, parser, mutable=mutable)
        if current
        else BeautifulSoup(html, parser)
    )


def parsed_json(content):
    current = _CURRENT.get()
    return current.json(content) if current else json.loads(content)


def snapshot_value(key, factory):
    """Reuse immutable derived results, scoped and bounded just like JSON input."""
    current = _CURRENT.get()
    if current is None:
        return factory()
    if key not in current.values:
        current.values[key] = factory()
        if len(current.values) > 64:
            current.values.popitem(last=False)
    else:
        current.values.move_to_end(key)
    return current.values[key]


def text_without(root, excluded=("script", "style")):
    """Read visible text without deleting nodes needed by other extractors."""
    return " ".join(
        str(node).strip()
        for node in root.descendants
        if type(node) is NavigableString
        and str(node).strip()
        and not any(parent.name in excluded for parent in node.parents)
    )
