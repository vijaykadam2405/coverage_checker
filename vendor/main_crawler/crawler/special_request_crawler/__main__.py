"""python -m special_request_crawler [target-name]"""
import asyncio
import contextlib

from .crawl import main

if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(main()))
