from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class DiscoveredURL:
    url: str
    normalized_url: str
    discovery_method: str
    depth: int
    parent_url: str | None = None
    anchor_text: str | None = None
    page_type: str = "HTML"
    capture_enabled: bool = True
