import { Panel } from "../common/Panel.jsx";
import { number, site } from "../../utils/format.js";
import { Badge } from "../common/Badge.jsx";
import { labels } from "../../config/status.js";
import { percentage } from "./Coverage.jsx";

export function Process({ snapshot, active }) {
  const run = snapshot.run;
  const checking = active?.action === "corpus";
  const ready =
    !!run.discovery_completed_at &&
    !["CANCELLED", "FAILED"].includes(run.status);
  const m = snapshot.metrics;
  const progress =
    !active && ["COMPLETED", "LIMITED"].includes(run.status) ? 100 : null;
  const latest = snapshot.latest_event;
  const phase = run.phase || "";
  const textStep = /Comparing HTML text/.test(phase);
  const corpusStep = /Checking corpus|Recovering corpus/.test(phase);
  return (
    <Panel
      title="Scan progress"
      subtitle={site(run.target_url)}
      action={
        <Badge status={active ? "processing" : "pending"}>
          {active
            ? checking
              ? "Checking corpus"
              : "Scanning & verifying"
            : labels[run.status] || run.status}
        </Badge>
      }
    >
      <ol className="process-steps">
        <li
          className={
            active && !corpusStep && !textStep ? "current" : ready ? "done" : ""
          }
        >
          1. Discover & validate
        </li>
        <li className={active && corpusStep ? "current" : ""}>
          2. Check Corpus
        </li>
        <li className={active && textStep ? "current" : ""}>
          3. Compare HTML text
        </li>
      </ol>
      <div
        className={`progress-track ${active && progress == null ? "indeterminate" : ""}`}
        role="progressbar"
        aria-label="Scan progress"
        aria-valuemin={0}
        aria-valuemax={100}
        {...(progress != null
          ? { "aria-valuenow": progress }
          : {
              "aria-valuetext": active
                ? "Processing one URL at a time; final website size unknown"
                : "Scan paused or awaiting analysis",
            })}
      >
        <span
          style={{
            width: progress == null ? (active ? "30%" : "0%") : `${progress}%`,
          }}
        />
      </div>
      <div className="progress-caption">
        <span>
          {number(m.discovered)} URLs saved · {number(m.processed ?? 0)}{" "}
          processed
          {" · "}
          {number(m.saved_corpus_checks ?? m.checked)} Corpus checks saved
          {" · "}
          {number(m.queued_urls ?? m.pending_website)} queued
        </span>
        <span>
          {progress == null
            ? active
              ? "Live"
              : "Saved"
            : percentage(progress)}
        </span>
      </div>
      <div className="current-event" aria-live="polite">
        <div>
          <span className="eyebrow">
            {active ? "CURRENT SCAN ACTIVITY" : "LAST RECORDED ACTIVITY"}
          </span>
          <strong>
            {active
              ? latest?.message || run.phase || "Waiting for next step"
              : labels[run.status] || run.phase || "Waiting for next step"}
          </strong>
          {active && snapshot.processing_urls?.length ? (
            snapshot.processing_urls.map((row) => (
              <div key={row.id} className="processing-url">
                <small>{row.url}</small>
                <small>
                  {row.corpus_state === "checking"
                    ? row.corpus_label
                    : row.website_label}{" "}
                  · {row.category} · Depth {row.navigation_depth ?? "unknown"}
                </small>
              </div>
            ))
          ) : (
            <small>{latest?.url || run.target_url}</small>
          )}
          {!active && (
            <small>{latest?.message || "No scan is currently running."}</small>
          )}
        </div>
      </div>
    </Panel>
  );
}
