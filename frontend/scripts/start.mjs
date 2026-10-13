// Start the compiled React app and Python API on one local port, not two servers.
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn, spawnSync } from "node:child_process";

const frontend = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const project = path.dirname(frontend);
const environmentFile = path.join(project, ".env");
if (existsSync(environmentFile)) process.loadEnvFile(environmentFile);
const port = Number(process.env.CRAWLSCOPE_PORT || 8000);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  console.error("CRAWLSCOPE_PORT must be a valid TCP port.");
  process.exit(1);
}
const url = `http://127.0.0.1:${port}`;
let existing;
try {
  existing = await fetch(`${url}/api/state`, {
    signal: AbortSignal.timeout(3000),
  });
} catch (error) {
  if (error.cause?.code !== "ECONNREFUSED") {
    console.error(
      `Cannot check ${url}. Another server may be using this port.`,
      error.message,
    );
    process.exit(1);
  }
}
if (existing) {
  const state = await existing.json().catch(() => null);
  if (
    existing.ok &&
    state?.application === "CrawlScope" &&
    state?.ui === "react"
  ) {
    console.log(
      `CrawlScope is already running: ${url}. Reusing the existing server; no additional port opened.`,
    );
    if (state.scan_workflow !== "serial-automatic") {
      console.log(
        "This server uses the older workflow. Stop it with Ctrl+C in its terminal, then run this command again to load the updated backend.",
      );
    }
    if (state.backend_update_available) {
      console.log(
        state.active
          ? "Backend source has changed. Pause the current scan in CrawlScope and wait for it to save, then stop the server with Ctrl+C and run this command again. Do not interrupt an active write."
          : "Backend source has changed. Stop this server with Ctrl+C in its terminal, then run this command again to load the latest code. Saved inventory is retained.",
      );
    }
    process.exit(0);
  }
  console.error(
    `Port ${port} is occupied by another service. It was not stopped.`,
  );
  process.exit(1);
}

const python = path.join(
  project,
  ".venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);
if (!existsSync(python)) {
  console.error(
    "Create the project .venv and install requirements.txt first. See README.md.",
  );
  process.exit(1);
}
if (!existsSync(path.join(frontend, "dist/index.html"))) {
  const vite = path.join(frontend, "node_modules/vite/bin/vite.js");
  if (!existsSync(vite)) {
    console.error(
      "Install frontend dependencies first: npm.cmd --prefix frontend ci",
    );
    process.exit(1);
  }
  const build = spawnSync(process.execPath, [vite, "build"], {
    cwd: frontend,
    stdio: "inherit",
    windowsHide: true,
  });
  if (build.status !== 0) process.exit(build.status || 1);
}
console.log(
  `Starting CrawlScope: ${url}\nReact renders the dashboard; Python runs only the API and crawler. Press Ctrl+C to stop.`,
);
const backend = spawn(python, ["dashboard.py"], {
  cwd: project,
  stdio: "inherit",
  windowsHide: true,
});
backend.on("error", (error) => {
  console.error(error.message);
  process.exit(1);
});
backend.on("exit", (code) => process.exit(code ?? 1));
