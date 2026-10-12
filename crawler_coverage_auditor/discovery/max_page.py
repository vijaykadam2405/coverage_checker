from __future__ import annotations

from collections.abc import Awaitable, Callable


class PaginationUnverified(RuntimeError):
    """A block or resource safeguard prevented establishing the actual boundary."""


async def find_maximum_page(is_valid: Callable[[int], Awaitable[bool]], *, start: int = 1,
                            maximum_probe: int = 1_048_576) -> int | None:
    """Use exponential probes then binary search; never hard-code a page-100 ceiling."""
    if not await is_valid(start):
        return None
    low, high = start, start * 2
    while high <= maximum_probe and await is_valid(high):
        low, high = high, high * 2
    if high > maximum_probe:
        raise PaginationUnverified(f"Pagination probe safeguard ({maximum_probe}) reached; maximum remains unknown")
    while low + 1 < high:
        middle = (low + high) // 2
        if await is_valid(middle):
            low = middle
        else:
            high = middle
    return low
