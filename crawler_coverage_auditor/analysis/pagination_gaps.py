from __future__ import annotations

import re
from collections import defaultdict


def contiguous_ranges(numbers: list[int]) -> list[tuple[int, int]]:
    if not numbers:
        return []
    result: list[tuple[int, int]] = []
    start = previous = sorted(set(numbers))[0]
    for number in sorted(set(numbers))[1:]:
        if number != previous + 1:
            result.append((start, previous))
            start = number
        previous = number
    result.append((start, previous))
    return result


def pagination_missing_ranges(rows: list[dict]) -> dict[str, list[tuple[int, int]]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        if row.get("validity") != "VALID_PAGE" or row.get("match_status") != "NOT_FOUND":
            continue
        match = re.search(r"[?&](?:page|paged|p|pg|pagina|pagenumber)=(\d+)", row.get("normalized_url", ""), re.I)
        if match:
            key = re.sub(r"([?&](?:page|paged|p|pg|pagina|pagenumber)=)\d+", r"\1N", row["normalized_url"], flags=re.I)
            groups[key].append(int(match.group(1)))
        else:
            match = re.search(r"/page/(\d+)(?:/|$)", row.get("normalized_url", ""))
            if match:
                key = re.sub(r"(/page/)\d+", r"\1N", row["normalized_url"])
                groups[key].append(int(match.group(1)))
    return {key: contiguous_ranges(values) for key, values in groups.items()}
