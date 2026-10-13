import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

const execute = promisify(execFile);
const script = fileURLToPath(new URL("../scripts/start.mjs", import.meta.url));

async function withServer(body, run) {
  const server = createServer((request, response) => {
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify(body));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    await run(server.address().port);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test("launcher reuses an existing React dashboard without creating another server", async () => {
  await withServer({ application: "CrawlScope", ui: "react" }, async (port) => {
    const result = await execute(process.execPath, [script], {
      env: { ...process.env, CRAWLSCOPE_PORT: String(port) },
    });
    assert.match(result.stdout, /already running/);
    assert.match(result.stdout, /no additional port opened/);
  });
});

test("launcher refuses to stop or replace an unrelated service", async () => {
  await withServer({ application: "Other application" }, async (port) => {
    await assert.rejects(
      execute(process.execPath, [script], {
        env: { ...process.env, CRAWLSCOPE_PORT: String(port) },
      }),
      (error) =>
        error.code === 1 && /occupied by another service/.test(error.stderr),
    );
    assert.equal((await fetch(`http://127.0.0.1:${port}/`)).status, 200);
  });
});

test("launcher validates ports rather than opening a different random port", async () => {
  await assert.rejects(
    execute(process.execPath, [script], {
      env: { ...process.env, CRAWLSCOPE_PORT: "invalid" },
    }),
    (error) => error.code === 1 && /valid TCP port/.test(error.stderr),
  );
});

test("launcher reports stale backend code without interrupting an active scan", async () => {
  await withServer(
    {
      application: "CrawlScope",
      ui: "react",
      scan_workflow: "serial-automatic",
      backend_update_available: true,
      active: { run_id: 3 },
    },
    async (port) => {
      const result = await execute(process.execPath, [script], {
        env: { ...process.env, CRAWLSCOPE_PORT: String(port) },
      });
      assert.match(result.stdout, /Pause the current scan/);
      assert.match(result.stdout, /Do not interrupt an active write/);
      assert.equal((await fetch(`http://127.0.0.1:${port}/`)).status, 200);
    },
  );
});
