# CrawlScope React frontend

React + Vite, JavaScript/JSX/CSS; no Python UI, Streamlit, routing/state/chart library
or added runtime dependency. The local Python backend serves the production bundle.

```powershell
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run check
npm.cmd --prefix frontend start
```

Normal application: http://127.0.0.1:8000. The launcher reuses CrawlScope on that
port and refuses to kill unrelated services. Rebuild after edits and restart the
backend when its Python code changes. Optional development: Vite `run dev` on 5173.

```text
src/
  main.jsx, App.jsx            React root and simple page state
  pages/                      Overview, Inventory, Analysis, Activity, ControlRoom
  components/common/          Panels, accessible dialogs, badges, charts, tables
  components/layout/          Sidebar and header
  components/coverage/        HTML coverage donut, stacked bars, exact scan progress
  components/inventory/       URL evidence dialog
  components/scan/            Selected scan history and explicit job actions
  hooks/                      Lightweight polling and bounded cancellable reads
  services/api.js             Local API client; reads never start jobs
  config/                     Labels, navigation, capture and supported controls
  utils/                      Formatting and server query encoding
  styles/                     Reused layout + responsive monitoring styles
tests/                        API, query encoding and single-port launcher tests
scripts/start.mjs             Application launcher
```

Start website scan automatically performs discovery, Corpus presence and HTML text
comparison one URL at a time. Pause/resume is a single scan action; there is no
separate Corpus Check button or concurrency/batch-size control. A backend with the
older staged workflow shows a restart warning and cannot start new scans from this UI.

Inventory retrieves only 50 rows per request; filtering and full CSV export happen
on the server. Detail evidence is lazy. Live URL activity is limited to 20 records,
activity to 200 displayed events with incremental cursors. Analysis is entry/manual
refresh only. Four-breakpoint Playwright checks live in `../tests/test_react_browser.py`;
the read-only review helper is `../scripts/preview_react.py`.

Do not edit generated `dist/` or treat `node_modules/` as application source.
Backend prerequisites, formulas, safety boundaries and API contracts are documented
in the root README and `../docs/IMPLEMENTATION_AUDIT.md`.
