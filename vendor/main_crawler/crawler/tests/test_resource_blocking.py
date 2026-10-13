"""The render tier must not download what a text crawler never reads.

LEARNED FROM SCRAPLING. Benchmarked 2026-08-18 over 30 tender portals it averaged 67s per
source against our 178s. Reading its engine, the cheapest difference is not cleverness — it
blocks ten resource types at the route layer by default
(scrapling/engines/constants.py::EXTRA_RESOURCES, wired in
toolbelt/navigation.py::create_intercept_handler) and documents ~25% faster fetches. We were
blocking nothing: every hero image, webfont and analytics beacon on a government portal was
fetched in full so that it could be discarded.

Scripts are deliberately NOT blocked — they are what renders the tender tables. Stylesheets
are, because layout still computes without them and nothing we extract reads CSS.

    python -m pytest tests/test_resource_blocking.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crawler.fetcher import _USELESS_RESOURCES, _block_useless_resources  # noqa: E402


class FakeRoute:
    def __init__(self, kind, url="https://x.test/a"):
        self.request = type("R", (), {"resource_type": kind, "url": url})()
        self.action = None

    def abort(self):
        self.action = "abort"

    def continue_(self):
        self.action = "continue"


class FakePage:
    def __init__(self):
        self.handler = None

    def route(self, pattern, handler):
        assert pattern == "**/*"
        self.handler = handler


def test_the_expensive_types_are_blocked():
    for kind in ("image", "font", "media", "stylesheet", "beacon"):
        assert kind in _USELESS_RESOURCES, kind


def test_script_and_document_are_never_blocked():
    """Blocking scripts would break every JS-rendered tender table — the whole reason the
    render tier exists. Blocking xhr/fetch would break the portals that load rows by XHR."""
    for kind in ("script", "document", "xhr", "fetch"):
        assert kind not in _USELESS_RESOURCES, kind


def test_handler_aborts_only_the_useless():
    page = FakePage()
    _block_useless_resources(page)
    assert page.handler is not None, "no route installed"
    for kind, expected in (("image", "abort"), ("font", "abort"), ("stylesheet", "abort"),
                           ("script", "continue"), ("document", "continue"),
                           ("xhr", "continue")):
        r = FakeRoute(kind)
        page.handler(r)
        assert r.action == expected, f"{kind} -> {r.action}, expected {expected}"


def test_a_broken_route_layer_never_costs_the_page():
    """Best-effort: speed is worth having, never worth losing the fetch it was speeding up."""
    class Exploding:
        def route(self, *a, **k):
            raise RuntimeError("no route support")
    _block_useless_resources(Exploding())      # must not raise


def test_it_can_be_switched_off():
    import os
    page = FakePage()
    os.environ["CRAWLER_BLOCK_RESOURCES"] = "0"
    try:
        _block_useless_resources(page)
        assert page.handler is None, "env knob did not disable blocking"
    finally:
        os.environ["CRAWLER_BLOCK_RESOURCES"] = "1"
