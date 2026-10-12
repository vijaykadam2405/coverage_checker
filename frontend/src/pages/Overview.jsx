import { time, period } from "../utils/format.js";
import { TYPES } from "../config/capture.js";
import { Process } from "../components/coverage/ScanProgress.jsx";
import { Coverage } from "../components/coverage/Coverage.jsx";
import { Panel } from "../components/common/Panel.jsx";
import { Badge } from "../components/common/Badge.jsx";
import { TextCoverageCell } from "../components/inventory/TextCoverageCell.jsx";
import { useQuery } from "../hooks/useQuery.js";

export function Overview({ snapshot, onDetail }) {
  const { data: live, error } = useQuery(
    `/runs/${snapshot.run.id}/live-urls`,
    snapshot.active ? 2000 : 5000,
  );
  return (
    <>
      {snapshot.run.needs_api_check && (
        <p className="info-box">
          Historical Corpus results. Use Update saved analysis to refresh URL
          presence and compare available saved HTML text without re-crawling.
        </p>
      )}
      <Process
        snapshot={snapshot}
        active={
          snapshot.active?.run_id === snapshot.run.id ? snapshot.active : null
        }
      />
      <Coverage snapshot={snapshot} />
      <Panel
        title="Live URL activity"
        subtitle="Latest collected or processed URLs · updates automatically during a scan"
      >
        {error && (
          <p role="alert" className="table-empty">
            {error}
          </p>
        )}
        <div className="table-scroll">
          <table className="live-url-table">
            <thead>
              <tr>
                <th>URL</th>
                <th>Type</th>
                <th>Website status</th>
                <th>Corpus status</th>
                <th>Text coverage</th>
                <th>Depth</th>
                <th>Period</th>
              </tr>
            </thead>
            <tbody>
              {(live?.items || []).map((row) => (
                <tr key={row.id}>
                  <td>
                    <button
                      className="url-button"
                      onClick={() => onDetail(row)}
                    >
                      <span>{row.url}</span>
                      {row.title && <small>{row.title}</small>}
                    </button>
                  </td>
                  <td>{TYPES[row.category] || row.category}</td>
                  <td>
                    <Badge status={row.website_state}>
                      {row.website_label}
                    </Badge>
                  </td>
                  <td>
                    <Badge status={row.corpus_state}>{row.corpus_label}</Badge>
                  </td>
                  <td>
                    <TextCoverageCell row={row} />
                  </td>
                  <td>{row.navigation_depth ?? "Unknown"}</td>
                  <td>
                    <time
                      dateTime={row.updated_at}
                      title={time(row.updated_at)}
                    >
                      {period(row.updated_at)}
                    </time>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!live?.items?.length && (
          <p className="table-empty">No URL activity recorded yet.</p>
        )}
        <p className="footnote">
          Text comparison applies to HTML only. File links are URL-only; Corpus
          text is compared with the saved website snapshot. Scores estimate word
          coverage; unavailable or truncated text is not assigned a percentage.
        </p>
      </Panel>
    </>
  );
}
