import asyncio

from crawler_coverage_auditor.discovery.max_page import find_maximum_page


def test_max_page_stops_when_out_of_range_pages_repeat_last_content() -> None:
    # The auditor's page probe supplies False after detecting that N and N-1
    # have the same fingerprint, modelling a 200-OK repeated-last-page site.
    async def valid(page: int) -> bool:
        return page <= 487

    assert asyncio.run(find_maximum_page(valid)) == 487
