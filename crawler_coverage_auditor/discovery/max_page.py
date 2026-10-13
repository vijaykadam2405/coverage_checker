from __future__ import annotations

from collections.abc import Awaitable, Callable


class PaginationUnverified(RuntimeError):
    """A block or inconclusive response prevented establishing the actual boundary."""


async def find_maximum_page(is_valid: Callable[[int], Awaitable[bool]], *, start: int = 1,
                            maximum_probe: int | None = None) -> int | None:
    """Probe until the actual boundary, not an arbitrary maximum page number.

    The historical maximum_probe argument is accepted but no longer truncates discovery.
    The caller must reject blocks/repeated responses and support user cancellation.
    """
    if not await is_valid(start):
        return None
    low, high = start, start * 2
    while await is_valid(high):
        low, high = high, high * 2
    while low + 1 < high:
        middle = (low + high) // 2
        if await is_valid(middle):
            low = middle
        else:
            high = middle
    return low
