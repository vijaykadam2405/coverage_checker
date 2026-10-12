from __future__ import annotations

from bs4 import BeautifulSoup


def extract_text(html: str) -> tuple[str, str | None, str | None]:
    """Meaningful HTML text, including preloaded tab panels, without nav/script noise."""
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript", "template", "svg"]):
        node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    h1 = soup.find("h1")
    heading = h1.get_text(" ", strip=True) if h1 else None
    body = soup.find("main") or soup.body or soup
    for node in body.select("nav, header, footer, aside"):
        node.decompose()
    return body.get_text(" ", strip=True), title, heading
