import asyncio

from crawler_coverage_auditor.discovery.max_page import find_maximum_page
from crawler_coverage_auditor.discovery.pagination import replace_page_parameter


def test_replaces_page_parameter() -> None:
    assert replace_page_parameter("https://example.test/news?year=2020&page=1", "page", 347).endswith("year=2020&page=347")


def test_maximum_page_can_exceed_one_hundred() -> None:
    async def valid(page: int) -> bool:
        return page <= 487
    assert asyncio.run(find_maximum_page(valid)) == 487
