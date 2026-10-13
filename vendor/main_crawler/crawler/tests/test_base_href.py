"""<base href> outranks the retrieval URL when resolving relative links (RFC 3986 5.1.1).

Regression from service.bund.de: it serves <base href="/"> and writes tender links relative,
so resolving against the page path invented /…/Ausschreibungen/Suche/IMPORTE/Ausschreibungen/…
— 'Ausschreibungen' twice, which the repeated-segment trap guard then dropped. 92 links lost.
"""
from crawler.parse import extract_links, extract_links_with_text

PAGE_URL = "https://www.service.bund.de/Content/DE/Ausschreibungen/Suche/Formular.html"
HTML = ('<html><head><base href="/"/></head><body>'
        '<a href="IMPORTE/Ausschreibungen/dtvp/2026/07/336140.html">Tender</a>'
        '</body></html>')
WANT = "https://www.service.bund.de/IMPORTE/Ausschreibungen/dtvp/2026/07/336140.html"


def test_base_href_wins_over_page_path():
    assert extract_links(HTML, PAGE_URL) == [WANT]
    assert extract_links_with_text(HTML, PAGE_URL)[0][0] == WANT


def test_no_base_tag_still_resolves_against_the_page():
    plain = '<a href="sibling.html">x</a>'
    assert extract_links(plain, PAGE_URL) == [
        "https://www.service.bund.de/Content/DE/Ausschreibungen/Suche/sibling.html"]


def test_empty_base_href_is_ignored():
    empty = '<base href="">' + '<a href="sibling.html">x</a>'
    assert extract_links(empty, PAGE_URL) == [
        "https://www.service.bund.de/Content/DE/Ausschreibungen/Suche/sibling.html"]


if __name__ == "__main__":
    test_base_href_wins_over_page_path()
    test_no_base_tag_still_resolves_against_the_page()
    test_empty_base_href_is_ignored()
    print("ok")
