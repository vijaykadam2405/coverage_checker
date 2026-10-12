# Active source import map

This snapshot predates the 11 October serial-workflow update. The unused `Metric.jsx`
component and its imports were removed; package startup now loads optional `.env`
through `python-dotenv`, and HTML comparison uses `validation/text_coverage.py`.
See SERIAL_WORKFLOW_REVIEW.md for the current change set.

Generated after source cleanup from actual import statements; reviewed with runtime callers.
Imports inside functions are included. This is a static source map, not a claim that
every branch executes in every run. Dynamic entry points and compatibility helpers
are noted below. Tests, CSS/templates, dependencies and generated artifacts are not
Python/JavaScript import nodes.

| Active file | Imports (local / external modules) | Direct local callers |
| --- | --- | --- |
| audit.py | crawler_coverage_auditor.main | Entry/package support or tests; see notes |
| crawler_coverage_auditor/__init__.py | None | Entry/package support or tests; see notes |
| crawler_coverage_auditor/analysis/__init__.py | None | Entry/package support or tests; see notes |
| crawler_coverage_auditor/analysis/coverage.py | __future__, collections, collections.abc | crawler_coverage_auditor/analysis/dashboard.py, crawler_coverage_auditor/analysis/gaps.py, crawler_coverage_auditor/analysis/inventory_segments.py, crawler_coverage_auditor/analysis/inventory.py, crawler_coverage_auditor/analysis/regression.py, crawler_coverage_auditor/analysis/states.py, crawler_coverage_auditor/auditor.py, crawler_coverage_auditor/reporting/excel.py, crawler_coverage_auditor/services/queries.py |
| crawler_coverage_auditor/analysis/dashboard.py | __future__, collections, urllib.parse, crawler_coverage_auditor.analysis.coverage, crawler_coverage_auditor.analysis.inventory_segments, crawler_coverage_auditor.analysis.pagination_gaps, crawler_coverage_auditor.analysis.root_cause, crawler_coverage_auditor.discovery.pagination, crawler_coverage_auditor.validation.normalize, crawler_coverage_auditor.analysis.inventory, json | crawler_coverage_auditor/reporting/html.py, crawler_coverage_auditor/services/queries.py |
| crawler_coverage_auditor/analysis/diagnostics.py | collections | crawler_coverage_auditor/services/queries.py |
| crawler_coverage_auditor/analysis/gaps.py | __future__, collections.abc, crawler_coverage_auditor.analysis.coverage | crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/analysis/inventory_segments.py | __future__, collections, urllib.parse, crawler_coverage_auditor.analysis.coverage, json, re | crawler_coverage_auditor/analysis/dashboard.py, crawler_coverage_auditor/analysis/root_cause.py, crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/analysis/inventory.py | collections, crawler_coverage_auditor.analysis.coverage, crawler_coverage_auditor.config | crawler_coverage_auditor/analysis/dashboard.py |
| crawler_coverage_auditor/analysis/pagination_gaps.py | __future__, collections, re | crawler_coverage_auditor/analysis/dashboard.py, crawler_coverage_auditor/analysis/root_cause.py |
| crawler_coverage_auditor/analysis/regression.py | __future__, crawler_coverage_auditor.analysis.coverage | crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/analysis/root_cause.py | __future__, crawler_coverage_auditor.analysis.pagination_gaps, crawler_coverage_auditor.analysis.inventory_segments | crawler_coverage_auditor/analysis/dashboard.py, crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/analysis/states.py | crawler_coverage_auditor.analysis.coverage | crawler_coverage_auditor/services/queries.py, crawler_coverage_auditor/web/api.py |
| crawler_coverage_auditor/auditor.py | __future__, contextlib, dataclasses, pathlib, urllib.parse, urllib.robotparser, bs4, crawler_coverage_auditor.analysis.coverage, crawler_coverage_auditor.analysis.gaps, crawler_coverage_auditor.analysis.root_cause, crawler_coverage_auditor.analysis.inventory_segments, crawler_coverage_auditor.analysis.regression, crawler_coverage_auditor.config, crawler_coverage_auditor.corpus.errors, crawler_coverage_auditor.corpus.coverage_api, crawler_coverage_auditor.discovery.browser, crawler_coverage_auditor.discovery.filters, crawler_coverage_auditor.discovery.max_page, crawler_coverage_auditor.discovery.pagination, crawler_coverage_auditor.discovery.resources, crawler_coverage_auditor.discovery.evidence, crawler_coverage_auditor.discovery.sitemap, crawler_coverage_auditor.discovery.static_links, crawler_coverage_auditor.reporting.excel, crawler_coverage_auditor.reporting.html, crawler_coverage_auditor.storage.database, crawler_coverage_auditor.storage.models, crawler_coverage_auditor.validation.fingerprints, crawler_coverage_auditor.validation.http, crawler_coverage_auditor.validation.normalize, crawler_coverage_auditor.validation.soft404, crawler_coverage_auditor.validation.text_extract, crawler_coverage_auditor.validation.access, crawler_coverage_auditor.validation.site_session, crawler_coverage_auditor.validation.network_safety, asyncio, csv, logging, os, json | crawler_coverage_auditor/main.py, crawler_coverage_auditor/services/jobs.py |
| crawler_coverage_auditor/config.py | __future__, dataclasses, pathlib, urllib.parse, crawler_coverage_auditor.corpus.credentials, crawler_coverage_auditor.validation.network_safety | crawler_coverage_auditor/analysis/inventory.py, crawler_coverage_auditor/auditor.py, crawler_coverage_auditor/discovery/browser.py, crawler_coverage_auditor/main.py, crawler_coverage_auditor/services/jobs.py, crawler_coverage_auditor/services/queries.py, crawler_coverage_auditor/web/api.py |
| crawler_coverage_auditor/corpus/__init__.py | None | Entry/package support or tests; see notes |
| crawler_coverage_auditor/corpus/coverage_api.py | __future__, datetime, email.utils, urllib.parse, .credentials, .errors, asyncio, json, httpx | crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/corpus/credentials.py | __future__, pathlib, ctypes, getpass, json, os, re, base64 | crawler_coverage_auditor/config.py, crawler_coverage_auditor/corpus/coverage_api.py, crawler_coverage_auditor/main.py, crawler_coverage_auditor/services/jobs.py, crawler_coverage_auditor/web/api.py |
| crawler_coverage_auditor/corpus/errors.py | None | crawler_coverage_auditor/auditor.py, crawler_coverage_auditor/corpus/coverage_api.py |
| crawler_coverage_auditor/discovery/__init__.py | None | Entry/package support or tests; see notes |
| crawler_coverage_auditor/discovery/browser.py | __future__, dataclasses, urllib.parse, crawler_coverage_auditor.discovery.resources, crawler_coverage_auditor.discovery.evidence, crawler_coverage_auditor.discovery.static_links, crawler_coverage_auditor.validation.fingerprints, crawler_coverage_auditor.validation.access, crawler_coverage_auditor.discovery.tabs, crawler_coverage_auditor.validation.text_extract, bs4, crawler_coverage_auditor.config, playwright.async_api, crawler_coverage_auditor.validation.network_safety, asyncio, re, inspect | crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/discovery/evidence.py | __future__, urllib.parse, bs4, crawler_coverage_auditor.discovery.resources, crawler_coverage_auditor.discovery.static_links, re | crawler_coverage_auditor/auditor.py, crawler_coverage_auditor/discovery/browser.py |
| crawler_coverage_auditor/discovery/filters.py | __future__, dataclasses, urllib.parse, bs4, re | crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/discovery/max_page.py | __future__, collections.abc | crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/discovery/pagination.py | __future__, dataclasses, urllib.parse, bs4, re | crawler_coverage_auditor/analysis/dashboard.py, crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/discovery/resources.py | __future__, urllib.parse, crawler_coverage_auditor.discovery.static_links, pathlib, json, re | crawler_coverage_auditor/auditor.py, crawler_coverage_auditor/discovery/browser.py, crawler_coverage_auditor/discovery/evidence.py, crawler_coverage_auditor/validation/http.py |
| crawler_coverage_auditor/discovery/sitemap.py | __future__, dataclasses, urllib.parse, xml.etree, crawler_coverage_auditor.validation.http, re, gzip | crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/discovery/static_links.py | __future__, dataclasses, urllib.parse, bs4, re | crawler_coverage_auditor/auditor.py, crawler_coverage_auditor/discovery/browser.py, crawler_coverage_auditor/discovery/evidence.py, crawler_coverage_auditor/discovery/resources.py |
| crawler_coverage_auditor/discovery/tabs.py | __future__, dataclasses, urllib.parse, bs4, re | crawler_coverage_auditor/discovery/browser.py |
| crawler_coverage_auditor/main.py | __future__, pathlib, crawler_coverage_auditor.auditor, crawler_coverage_auditor.config, crawler_coverage_auditor.corpus.credentials, argparse, asyncio, logging | audit.py |
| crawler_coverage_auditor/reporting/__init__.py | None | Entry/package support or tests; see notes |
| crawler_coverage_auditor/reporting/excel.py | __future__, pathlib, openpyxl, openpyxl.styles, crawler_coverage_auditor.analysis.coverage | crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/reporting/html.py | __future__, pathlib, typing, crawler_coverage_auditor.analysis.dashboard, json | crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/services/__init__.py | None | Entry/package support or tests; see notes |
| crawler_coverage_auditor/services/inventory.py | __future__, contextlib, pathlib, typing, json, sqlite3 | crawler_coverage_auditor/services/queries.py |
| crawler_coverage_auditor/services/jobs.py | __future__, dataclasses, pathlib, crawler_coverage_auditor.auditor, crawler_coverage_auditor.config, crawler_coverage_auditor.corpus.credentials, crawler_coverage_auditor.services.queries, asyncio, threading, sys, json | crawler_coverage_auditor/web/api.py |
| crawler_coverage_auditor/services/queries.py | __future__, contextlib, pathlib, urllib.parse, crawler_coverage_auditor.analysis.coverage, crawler_coverage_auditor.analysis.dashboard, crawler_coverage_auditor.analysis.diagnostics, crawler_coverage_auditor.analysis.states, crawler_coverage_auditor.config, crawler_coverage_auditor.services.inventory, crawler_coverage_auditor.validation.site_session, csv, io, json, math, os | crawler_coverage_auditor/services/jobs.py |
| crawler_coverage_auditor/storage/__init__.py | None | Entry/package support or tests; see notes |
| crawler_coverage_auditor/storage/database.py | __future__, datetime, pathlib, typing, crawler_coverage_auditor.storage.models, urllib.parse, collections, json, sqlite3 | crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/storage/models.py | __future__, dataclasses | crawler_coverage_auditor/auditor.py, crawler_coverage_auditor/storage/database.py |
| crawler_coverage_auditor/validation/__init__.py | None | Entry/package support or tests; see notes |
| crawler_coverage_auditor/validation/access.py | __future__, dataclasses, bs4, re | crawler_coverage_auditor/auditor.py, crawler_coverage_auditor/discovery/browser.py, crawler_coverage_auditor/validation/site_session.py |
| crawler_coverage_auditor/validation/fingerprints.py | __future__, hashlib, re | crawler_coverage_auditor/auditor.py, crawler_coverage_auditor/discovery/browser.py |
| crawler_coverage_auditor/validation/http.py | __future__, urllib.parse, collections.abc, datetime, email.utils, dataclasses, http.cookiejar, urllib.request, crawler_coverage_auditor.validation.site_session, crawler_coverage_auditor.validation.network_safety, crawler_coverage_auditor.discovery.resources, asyncio, random, inspect, httpx | crawler_coverage_auditor/auditor.py, crawler_coverage_auditor/discovery/sitemap.py |
| crawler_coverage_auditor/validation/network_safety.py | urllib.parse, asyncio, ipaddress, os, socket | crawler_coverage_auditor/auditor.py, crawler_coverage_auditor/config.py, crawler_coverage_auditor/discovery/browser.py, crawler_coverage_auditor/validation/http.py, crawler_coverage_auditor/validation/site_session.py, crawler_coverage_auditor/web/api.py |
| crawler_coverage_auditor/validation/normalize.py | __future__, collections.abc, urllib.parse | crawler_coverage_auditor/analysis/dashboard.py, crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/validation/site_session.py | __future__, http.cookiejar, pathlib, urllib.parse, ctypes, crawler_coverage_auditor.validation.network_safety, playwright.sync_api, crawler_coverage_auditor.validation.access, argparse, hashlib, json, os, sys, time, tempfile | crawler_coverage_auditor/auditor.py, crawler_coverage_auditor/services/queries.py, crawler_coverage_auditor/validation/http.py |
| crawler_coverage_auditor/validation/soft404.py | __future__, dataclasses | crawler_coverage_auditor/auditor.py |
| crawler_coverage_auditor/validation/text_extract.py | __future__, bs4 | crawler_coverage_auditor/auditor.py, crawler_coverage_auditor/discovery/browser.py |
| crawler_coverage_auditor/web/__init__.py | None | Entry/package support or tests; see notes |
| crawler_coverage_auditor/web/api.py | __future__, pathlib, typing, fastapi, fastapi.middleware.cors, fastapi.responses, fastapi.staticfiles, pydantic, starlette.middleware.trustedhost, crawler_coverage_auditor.analysis.states, crawler_coverage_auditor.config, crawler_coverage_auditor.corpus.credentials, crawler_coverage_auditor.services, crawler_coverage_auditor.services.jobs, crawler_coverage_auditor.validation.network_safety, json, os, threading | dashboard.py |
| dashboard.py | crawler_coverage_auditor.web.api, os, uvicorn | Entry/package support or tests; see notes |
| frontend/scripts/start.mjs | node:fs, node:path, node:url, node:child_process | Entry/package support or tests; see notes |
| frontend/src/App.jsx | react, lucide-react, ./hooks/useDashboard.js, ./hooks/useQuery.js, ./services/api.js, ./components/layout/Sidebar.jsx, ./components/layout/Topbar.jsx, ./components/scan/ScanToolbar.jsx, ./components/inventory/UrlEvidenceDialog.jsx, ./components/common/Empty.jsx, ./pages/Overview.jsx, ./pages/Inventory.jsx, ./pages/Analysis.jsx, ./pages/Activity.jsx, ./pages/ControlRoom.jsx, ./config/capture.js, ./utils/format.js, ./config/navigation.js | frontend/src/main.jsx |
| frontend/src/components/common/Badge.jsx | ../../config/status.js | frontend/src/components/coverage/ScanProgress.jsx, frontend/src/components/inventory/UrlEvidenceDialog.jsx, frontend/src/components/scan/ScanToolbar.jsx, frontend/src/pages/Inventory.jsx, frontend/src/pages/Overview.jsx |
| frontend/src/components/common/Bars.jsx | ../../utils/format.js | frontend/src/pages/Analysis.jsx, frontend/src/pages/Overview.jsx |
| frontend/src/components/common/DataTable.jsx | ../../utils/format.js | frontend/src/pages/Activity.jsx, frontend/src/pages/Analysis.jsx |
| frontend/src/components/common/Dialog.jsx | react, lucide-react | frontend/src/components/inventory/UrlEvidenceDialog.jsx |
| frontend/src/components/common/Empty.jsx | lucide-react | frontend/src/App.jsx |
| frontend/src/components/common/ErrorBoundary.jsx | react | frontend/src/main.jsx |
| frontend/src/components/common/Panel.jsx | None | frontend/src/components/coverage/Coverage.jsx, frontend/src/components/coverage/ScanProgress.jsx, frontend/src/pages/Activity.jsx, frontend/src/pages/Analysis.jsx, frontend/src/pages/ControlRoom.jsx, frontend/src/pages/Inventory.jsx, frontend/src/pages/Overview.jsx |
| frontend/src/components/coverage/Coverage.jsx | ../common/Panel.jsx, ../../utils/format.js | frontend/src/components/coverage/CoverageBars.jsx, frontend/src/components/coverage/ScanProgress.jsx, frontend/src/pages/Analysis.jsx, frontend/src/pages/Overview.jsx |
| frontend/src/components/coverage/CoverageBars.jsx | ./Coverage.jsx | frontend/src/pages/Analysis.jsx |
| frontend/src/components/coverage/ScanProgress.jsx | ../common/Panel.jsx, ../../utils/format.js, ../common/Badge.jsx, ../../config/status.js, ./Coverage.jsx | frontend/src/pages/Overview.jsx |
| frontend/src/components/inventory/UrlEvidenceDialog.jsx | lucide-react, ../../config/capture.js, ../common/Dialog.jsx, ../common/Badge.jsx | frontend/src/App.jsx |
| frontend/src/components/layout/Sidebar.jsx | lucide-react, ../../config/navigation.js, react | frontend/src/App.jsx |
| frontend/src/components/layout/Topbar.jsx | lucide-react, ../../config/navigation.js | frontend/src/App.jsx |
| frontend/src/components/scan/ScanToolbar.jsx | lucide-react, ../common/Badge.jsx, ../../config/status.js, ../../utils/format.js | frontend/src/App.jsx |
| frontend/src/config/capture.js | lucide-react | frontend/src/App.jsx, frontend/src/components/inventory/UrlEvidenceDialog.jsx, frontend/src/pages/Analysis.jsx, frontend/src/pages/ControlRoom.jsx, frontend/src/pages/Inventory.jsx, frontend/src/pages/Overview.jsx |
| frontend/src/config/controls.js | None | frontend/src/pages/ControlRoom.jsx |
| frontend/src/config/navigation.js | lucide-react | frontend/src/App.jsx, frontend/src/components/layout/Sidebar.jsx, frontend/src/components/layout/Topbar.jsx |
| frontend/src/config/status.js | None | frontend/src/components/common/Badge.jsx, frontend/src/components/coverage/ScanProgress.jsx, frontend/src/components/scan/ScanToolbar.jsx |
| frontend/src/hooks/useDashboard.js | react, ../services/api.js | frontend/src/App.jsx |
| frontend/src/hooks/useQuery.js | react, ../services/api.js | frontend/src/App.jsx, frontend/src/pages/Analysis.jsx, frontend/src/pages/Inventory.jsx, frontend/src/pages/Overview.jsx |
| frontend/src/main.jsx | react-dom/client, ./App.jsx, ./components/common/ErrorBoundary.jsx | Entry/package support or tests; see notes |
| frontend/src/pages/Activity.jsx | react, ../components/common/Panel.jsx, ../components/common/DataTable.jsx, ../utils/format.js, ../services/api.js | frontend/src/App.jsx |
| frontend/src/pages/Analysis.jsx | ../components/common/Panel.jsx, ../components/common/DataTable.jsx, ../components/common/Bars.jsx, ../components/coverage/Coverage.jsx, ../components/coverage/CoverageBars.jsx, ../hooks/useQuery.js, ../config/capture.js, react | frontend/src/App.jsx |
| frontend/src/pages/ControlRoom.jsx | react, ../components/common/Panel.jsx, ../config/capture.js, ../config/controls.js | frontend/src/App.jsx |
| frontend/src/pages/Inventory.jsx | react, ../components/common/Panel.jsx, lucide-react, ../config/capture.js, ../utils/format.js, ../components/common/Badge.jsx, ../hooks/useQuery.js, ../utils/query.js | frontend/src/App.jsx |
| frontend/src/pages/Overview.jsx | ../utils/format.js, ../config/capture.js, ../components/coverage/ScanProgress.jsx, ../components/coverage/Coverage.jsx, ../components/common/Panel.jsx, ../components/common/Badge.jsx, ../components/inventory/TextCoverageCell.jsx, ../hooks/useQuery.js | frontend/src/App.jsx |
| frontend/src/services/api.js | None | frontend/src/App.jsx, frontend/src/hooks/useDashboard.js, frontend/src/hooks/useQuery.js, frontend/src/pages/Activity.jsx |
| frontend/src/utils/format.js | None | frontend/src/App.jsx, frontend/src/components/common/Bars.jsx, frontend/src/components/common/DataTable.jsx, frontend/src/components/coverage/Coverage.jsx, frontend/src/components/coverage/ScanProgress.jsx, frontend/src/components/scan/ScanToolbar.jsx, frontend/src/pages/Activity.jsx, frontend/src/pages/Inventory.jsx, frontend/src/pages/Overview.jsx |
| frontend/src/utils/query.js | None | frontend/src/pages/Inventory.jsx |
| frontend/vite.config.js | vite, @vitejs/plugin-react | Entry/package support or tests; see notes |

## Runtime and special callers

- audit.py → main.main; dashboard.py is launched by frontend/scripts/start.mjs.
- main.py and web/api.py use AuditJobs/CoverageAuditor; no Python UI rendering.
- corpus/credentials.py and validation/site_session.py are deliberate user-invoked CLI helpers.
- Package __init__.py files establish import packages, not dead runtime services.
- services/inventory.list_runs and load_events are tested historical/report compatibility
  helpers, not active polling; live read paths use services/queries.py.
- reporting/html.py reads reporting/templates/coverage.html; no remote frontend dependency.
- main.jsx imports styles/index.css → base/layout/dashboard/tables/dialogs/utilities/
  responsive/monitor.css. CSS responsibilities remain presentation only.
- frontend/index.html loads src/main.jsx in development; Vite replaces it with the
  generated bundle at build time. React uses simple view state, not a routing framework.
- Static import map supplements the full pre-edit source/caller audit. Removed modules
  are listed in IMPLEMENTATION_AUDIT.md; no active imports refer to those old paths.
