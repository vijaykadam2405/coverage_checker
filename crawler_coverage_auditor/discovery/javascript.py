"""Persistent Acorn subprocess: parse public source, never evaluate it."""

import asyncio
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class JavaScriptMiner:
    def __init__(self):
        self.process = None

    async def parse(self, source):
        executable = os.getenv("CRAWLSCOPE_NODE_PATH") or shutil.which("node")
        if not executable:
            return {
                "urls": [],
                "patterns": [],
                "error": "Node.js is unavailable; JavaScript source analysis not completed",
            }
        try:
            if not self.process or self.process.returncode is not None:
                self.process = await asyncio.create_subprocess_exec(
                    executable,
                    str(ROOT / "frontend/scripts/mine-javascript.mjs"),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=16 * 1024 * 1024,
                    **({"creationflags": 0x08000000} if os.name == "nt" else {}),
                )
            async with asyncio.timeout(20):
                self.process.stdin.write(
                    (json.dumps({"source": source}) + "\n").encode()
                )
                await self.process.stdin.drain()
                line = await self.process.stdout.readline()
                if not line:
                    raise ValueError(
                        "Acorn process unavailable; run npm install in frontend"
                    )
                return json.loads(line)
        except (OSError, ValueError, TimeoutError, BrokenPipeError) as error:
            await self.close()
            return {
                "urls": [],
                "patterns": [],
                "error": f"JavaScript analysis incomplete: {error}",
            }

    async def close(self):
        if self.process and self.process.returncode is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            await self.process.wait()
        self.process = None
