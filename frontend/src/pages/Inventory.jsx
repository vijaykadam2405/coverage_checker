import { useEffect, useState } from "react";
import { Panel } from "../components/common/Panel.jsx";
import {
  Search,
  ChevronLeft,
  ChevronRight,
  Download,
  ArrowUpRight,
} from "lucide-react";
import { TYPES } from "../config/capture.js";
import { number, time, period } from "../utils/format.js";
import { TextCoverageCell } from "../components/inventory/TextCoverageCell.jsx";
import { Badge } from "../components/common/Badge.jsx";
import { useQuery } from "../hooks/useQuery.js";
import { inventoryQuery } from "../utils/query.js";

export function Inventory({ snapshot, preset, onDetail, methods }) {
  const [filters, setFilters] = useState({
    type: "ALL",
    corpus_status: preset?.status || "all",
    sort: "url",
    order: "asc",
  });
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  useEffect(() => {
    if (preset) {
      setFilters((previous) => ({
        ...previous,
        type: "ALL",
        corpus_status: preset.status || "all",
      }));
      setPage(1);
    }
  }, [preset]);
  useEffect(() => {
    const timer = setTimeout(() => {
      setFilters((previous) => ({ ...previous, search: query }));
      setPage(1);
    }, 250);
    return () => clearTimeout(timer);
  }, [query]);
  const params = inventoryQuery(filters, page);
  const { data, error, loading } = useQuery(
    `/runs/${snapshot.run.id}/urls?${params}`,
    snapshot.active ? 3000 : 0,
  );
  function select(key, value) {
    setFilters((previous) => ({ ...previous, [key]: value }));
    setPage(1);
  }
  const options = (key, label, items) => (
    <label className="filter-field">
      {label}
      <select
        aria-label={label}
        value={filters[key] || ""}
        onChange={(event) => select(key, event.target.value)}
      >
        <option value="">All</option>
        {items.map(([value, text]) => (
          <option key={value} value={value}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );
  return (
    <Panel
      title="Discovered inventory"
      subtitle="All stored URLs for this inventory · search or select a URL to inspect its evidence"
      action={
        <a
          className="secondary"
          aria-disabled={!data?.total}
          href={`/api/runs/${snapshot.run.id}/urls/export?${inventoryQuery(filters)}`}
        >
          <Download size={16} />
          Export CSV
        </a>
      }
    >
      {data?.host_summary && (
        <div className="workspace-totals">
          <span>{filters.host || "All captured hosts"}</span>
          <span>
            URLs <b>{number(data.host_summary.discovered)}</b>
          </span>
          <span>
            Hosts <b>{number(data.host_summary.host_count)}</b>
          </span>
          <span>
            Valid HTML <b>{number(data.host_summary.html)}</b>
          </span>
          <span>
            File links <b>{number(data.host_summary.files)}</b>
          </span>
          <span>
            Confirmed missing <b>{number(data.host_summary.missing)}</b>
          </span>
          <span>
            Unverified <b>{number(data.host_summary.unverified)}</b>
          </span>
        </div>
      )}
      <div className="table-toolbar">
        <label className="search">
          <Search size={17} />
          <input
            aria-label="Search URLs"
            placeholder="Search URL or page title…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        {options(
          "host",
          "Host",
          snapshot.hosts.map((item) => [
            item.host,
            `${item.host} (${number(item.count)})`,
          ]),
        )}
        {options("sort", "Sort", [
          ["url", "URL"],
          ["title", "Title"],
          ["host", "Host"],
          ["type", "Type"],
          ["depth", "Depth"],
          ["status", "Corpus status"],
          ["http", "HTTP"],
          ["updated", "Updated"],
        ])}
        {options("order", "Order", [
          ["asc", "Ascending"],
          ["desc", "Descending"],
        ])}
      </div>
      <div className="status-filters">
        {[
          ["all", "All"],
          ["eligible", "Valid / collected"],
          ["found", "Found in corpus"],
          ["missing", "Missing from corpus"],
          ["unverified", "Not checked / needs review"],
          ["processing", "Processing"],
          ["failed", "Failed / excluded"],
        ].map(([status, label]) => (
          <button
            key={status}
            className={filters.corpus_status === status ? "selected" : ""}
            onClick={() => select("corpus_status", status)}
          >
            {label}
          </button>
        ))}
      </div>
      <details className="inventory-extra">
        <summary>More filters</summary>
        <div className="filter-grid">
          {options("validation_status", "Website validation", [
            ["VALID_PAGE", "Valid page / metadata"],
            ["URL_ONLY", "File URL collected"],
            ["HTTP_ERROR", "HTTP error"],
            ["SOFT_404", "Soft 404"],
            ["EMPTY_PAGE", "Empty HTML"],
            ["RENDER_REQUIRED", "Needs rendering"],
            ["BOT_BLOCKED", "Bot restriction"],
            ["LOGIN_REQUIRED", "Login required"],
            ["CAPTCHA_REQUIRED", "CAPTCHA required"],
            ["RATE_LIMITED", "Rate limited"],
            ["REPEATED_PAGE", "Repeated page"],
          ])}
          {options("method", "Discovery method", Object.entries(methods || {}))}
          <label className="filter-field">
            Navigation depth
            <input
              aria-label="Navigation depth"
              placeholder="Any, number, or unknown"
              value={filters.depth || ""}
              onChange={(event) => select("depth", event.target.value)}
            />
          </label>
          <label className="filter-field">
            HTTP status
            <input
              aria-label="HTTP status"
              placeholder="Any, e.g. 200 or 404"
              value={filters.http_status || ""}
              onChange={(event) => select("http_status", event.target.value)}
            />
          </label>
        </div>
      </details>
      <div className="scoped-summary">
        <span>{filters.host || "All captured hosts"}</span>
        <span>
          Discovered <b>{number(data?.summary.discovered)}</b>
        </span>
        <span>
          Eligible <b>{number(data?.summary.eligible)}</b>
        </span>
        <span>
          Found <b>{number(data?.summary.found)}</b>
        </span>
        <span>
          Missing <b>{number(data?.summary.missing)}</b>
        </span>
        <span>
          Unverified <b>{number(data?.summary.unverified)}</b>
        </span>
      </div>
      {error && (
        <p role="alert" className="table-empty">
          {error}
        </p>
      )}
      <div className="table-scroll" aria-busy={loading}>
        <table className="url-table">
          <thead>
            <tr>
              <th>URL / title</th>
              <th>Host / type</th>
              <th>Website</th>
              <th>Corpus</th>
              <th>Text coverage</th>
              <th>Depth</th>
              <th>Discovery / HTTP</th>
              <th>Period</th>
              <th>
                <span className="sr-only">Inspect</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(data?.items || []).map((row) => (
              <tr key={row.id}>
                <td>
                  <button className="url-button" onClick={() => onDetail(row)}>
                    <span>{row.title || row.url}</span>
                    <small>{row.url}</small>
                  </button>
                </td>
                <td>
                  {row.host}
                  <small className="cell-note">
                    {TYPES[row.category]} · {row.subtype}
                  </small>
                </td>
                <td>
                  <Badge status={row.website_state}>{row.website_label}</Badge>
                </td>
                <td>
                  <Badge status={row.corpus_state}>{row.corpus_label}</Badge>
                </td>
                <td>
                  <TextCoverageCell row={row} />
                </td>
                <td>{row.navigation_depth ?? "Unknown"}</td>
                <td>
                  {row.method_label}
                  <small className="cell-note">
                    HTTP {row.status_code ?? "Not fetched"}
                  </small>
                </td>
                <td>
                  <time dateTime={row.updated_at} title={time(row.updated_at)}>
                    {period(row.updated_at)}
                  </time>
                </td>
                <td>
                  <button
                    className="icon-button"
                    aria-label={`Inspect ${row.url}`}
                    onClick={() => onDetail(row)}
                  >
                    <ArrowUpRight size={17} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!data?.items?.length && (
        <p className="table-empty">
          {loading
            ? "Loading matching URLs…"
            : "No matching URLs. Try another search, host or status."}
        </p>
      )}
      <div className="table-footer">
        <span>
          {number(data?.total)} matching URLs · page {data?.page || page} of{" "}
          {data?.pages || 1} · up to 50 shown
        </span>
        <div>
          <button
            className="icon-button"
            aria-label="Previous page"
            disabled={page <= 1 || loading}
            onClick={() => setPage(page - 1)}
          >
            <ChevronLeft size={18} />
          </button>
          <button
            className="icon-button"
            aria-label="Next page"
            disabled={!data || page >= data.pages || loading}
            onClick={() => setPage(page + 1)}
          >
            <ChevronRight size={18} />
          </button>
        </div>
      </div>
      <p className="footnote">
        Counts reflect the current filters. Export includes every matching URL,
        not just this page. File URLs are not downloaded or availability-tested.
        External resource hosts are not separate target sites.
      </p>
    </Panel>
  );
}
