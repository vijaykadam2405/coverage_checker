import { useState } from "react";
import { Search, ArrowUpRight, Globe } from "lucide-react";
import { Panel } from "../components/common/Panel.jsx";
import { Badge } from "../components/common/Badge.jsx";
import { number, period, time } from "../utils/format.js";
import { percentage } from "../components/coverage/Coverage.jsx";

export function WebsiteWorkspace({ sites, selected, onOpen }) {
  const [query, setQuery] = useState("");
  const filtered = sites.filter((item) =>
    `${item.site} ${item.run.target_url}`
      .toLowerCase()
      .includes(query.toLowerCase().trim()),
  );
  return (
    <Panel
      title="Saved websites"
      subtitle={`${number(sites.length)} websites · latest saved inventory per website · click a website to view its URLs`}
      className="website-library"
    >
      <div className="table-toolbar">
        <label className="search">
          <Search size={17} />
          <input
            aria-label="Search saved websites"
            placeholder="Search saved websites…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <span className="library-count">
          {number(filtered.length)} websites
        </span>
      </div>
      <div className="table-scroll website-list">
        <table>
          <thead>
            <tr>
              <th>Website</th>
              <th>HTML pages</th>
              <th>PDFs</th>
              <th>Documents</th>
              <th>Total URLs</th>
              <th>HTML coverage</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((item) => (
              <tr
                key={item.site}
                className={item.run.id === selected ? "selected-row" : ""}
              >
                <td>
                  <button
                    className="url-button website-link"
                    onClick={() => onOpen(item.run.id, "inventory")}
                  >
                    <span>
                      <Globe size={15} />
                      {item.run.target_url}
                      <ArrowUpRight size={14} />
                    </span>
                    <small title={time(item.run.started_at)}>
                      Inventory #{item.run.id} · {period(item.run.started_at)}
                    </small>
                  </button>
                </td>
                <td>
                  {number(item.summary.counts.HTML)}
                  <small className="cell-note">
                    {number(item.summary.types.HTML.missing)} missing
                  </small>
                </td>
                <td>
                  {number(item.summary.counts.PDF)}
                  <small className="cell-note">
                    {number(item.summary.types.PDF.missing)} missing
                  </small>
                </td>
                <td>{number(item.summary.counts.DOCUMENT)}</td>
                <td>{number(item.summary.metrics.discovered)}</td>
                <td>
                  {percentage(item.summary.html_coverage.verified_coverage)}
                  <small className="cell-note">
                    {percentage(
                      item.summary.html_coverage.verification_completion,
                    )}{" "}
                    checked
                  </small>
                </td>
                <td>
                  <Badge
                    status={
                      item.run.status === "COMPLETED"
                        ? "found"
                        : item.run.status === "RUNNING"
                          ? "processing"
                          : "pending"
                    }
                  >
                    {item.run.status_label || item.run.status}
                  </Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!filtered.length && (
        <p className="table-empty">
          {sites.length
            ? "No saved websites match your search."
            : "No saved websites yet. Start a scan from Overview."}
        </p>
      )}
    </Panel>
  );
}
