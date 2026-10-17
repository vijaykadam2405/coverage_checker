import { Pause, Play } from "lucide-react";
import { Badge } from "../common/Badge.jsx";
import { labels } from "../../config/status.js";
import { site, time } from "../../utils/format.js";

export function ScanToolbar({
  runs,
  runId,
  run,
  active,
  working,
  connected,
  onSelect,
  onResume,
  onStop,
  showActions = true,
}) {
  const selectedActive = active?.run_id === runId;
  const canResume =
    (run &&
      [
        "AWAITING_CORPUS",
        "CORPUS_PAUSED",
        "CANCELLED",
        "FAILED",
        "RUNNING",
      ].includes(run.status)) ||
    (!!run?.discovery_completed_at && run.needs_api_check) ||
    run?.discovery_upgrade_available ||
    run?.discovery_work_remaining;
  return (
    <div className="run-toolbar">
      <div className="selected-inventory">
        <strong>{run ? site(run.target_url) : "No website selected"}</strong>
        {run && (
          <details className="scan-history">
            <summary>Saved scan history</summary>
            <div>
              {(runs.some((item) => item.id === run.id) ? runs : [run, ...runs])
                .filter(
                  (item) =>
                    site(item.target_url).replace(/^www\./, "") ===
                    site(run.target_url).replace(/^www\./, ""),
                )
                .map((item) => (
                  <button
                    key={item.id}
                    className="text-button"
                    disabled={working || item.id === runId}
                    onClick={() => onSelect(item.id)}
                  >
                    #{item.id} · {time(item.started_at)}
                    {item.id === runId ? " · Selected" : ""}
                  </button>
                ))}
            </div>
          </details>
        )}
      </div>
      {showActions && (
        <div className="run-actions">
          {run && (
            <Badge
              status={
                selectedActive
                  ? "processing"
                  : run.status === "AWAITING_CORPUS"
                    ? "pending"
                    : run.status === "COMPLETED"
                      ? "found"
                      : "warning"
              }
            >
              {selectedActive
                ? active.action === "corpus"
                  ? "Checking corpus"
                  : "Scanning & verifying"
                : labels[run.status] || run.status}
            </Badge>
          )}
          {selectedActive ? (
            <button
              className="secondary"
              disabled={working || active.stopping}
              onClick={onStop}
            >
              <Pause size={16} />
              {active.stopping ? "Saving & stopping…" : "Pause scan"}
            </button>
          ) : canResume ? (
            <button
              className="primary"
              disabled={!!active || working || !connected}
              onClick={onResume}
            >
              <Play size={16} />
              {run?.discovery_upgrade_available
                ? "Upgrade saved scan"
                : run?.needs_api_check
                  ? "Update saved analysis"
                  : "Resume scan"}
            </button>
          ) : null}
        </div>
      )}
    </div>
  );
}
