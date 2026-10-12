import { useEffect, useState } from "react";
import { Panel } from "../components/common/Panel.jsx";
import { DataTable } from "../components/common/DataTable.jsx";
import { time } from "../utils/format.js";
import { api } from "../services/api.js";

export function ActivityView({ snapshot }) {
  const seconds = Math.max(
    0,
    Math.floor(
      (new Date(snapshot.run.completed_at || Date.now()) -
        new Date(snapshot.run.started_at)) /
        1000,
    ),
  );
  const [filter, setFilter] = useState("");
  const [stage, setStage] = useState("");
  const [kind, setKind] = useState("");
  const [events, setEvents] = useState([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let stopped = false,
      timer,
      cursor = 0;
    const controller = new AbortController();
    setEvents([]);
    setError("");
    async function read() {
      try {
        const data = await api(
          `/runs/${snapshot.run.id}/activity?${new URLSearchParams({ limit: 80, after_id: cursor, severity: filter, stage, kind })}`,
          undefined,
          "GET",
          { signal: controller.signal },
        );
        if (stopped) return;
        cursor = data.cursor;
        setEvents((previous) =>
          [
            ...new Map(
              [...data.items, ...previous].map((item) => [item.id, item]),
            ).values(),
          ]
            .sort((a, b) => b.id - a.id)
            .slice(0, 200),
        );
        setError("");
        timer = setTimeout(read, data.has_more ? 400 : 3000);
      } catch (failure) {
        if (!stopped && failure.name !== "AbortError") {
          setError(failure.message);
          timer = setTimeout(read, 5000);
        }
      }
    }
    read();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [snapshot.run.id, filter, stage, kind]);
  return (
    <>
      <Panel title="Scan status" subtitle={snapshot.run.phase}>
        <div className="scoped-summary">
          <span>Started {time(snapshot.run.started_at)}</span>
          <span>
            Duration {Math.floor(seconds / 60)}m {seconds % 60}s
          </span>
          <span>
            {snapshot.metrics.discovered} discovered ·{" "}
            {snapshot.metrics.checked} checked · {snapshot.counters.warnings}{" "}
            warnings
          </span>
          <span>
            Discovery finished {time(snapshot.run.discovery_completed_at)}
          </span>
          <span>
            Verification started {time(snapshot.run.verification_started_at)}
          </span>
          <span>
            {snapshot.counters.errors} errors · {snapshot.counters.boundaries}{" "}
            boundaries
          </span>
        </div>
      </Panel>
      {snapshot.boundaries.length > 0 && (
        <Panel
          title="Coverage boundaries"
          subtitle="Most recent 20 restrictions; the full offline report retains all evidence"
        >
          <DataTable
            rows={snapshot.boundaries.map((item) => ({
              Stage: item.stage,
              URL: item.url,
              Reason: item.reason,
              "Last successful URL": item.last_successful_url,
              Time: time(item.created_at),
            }))}
          />
        </Panel>
      )}
      <Panel
        title="Live activity"
        subtitle="Incremental saved events · bounded to the latest 200 displayed"
        action={
          <select
            aria-label="Activity severity"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          >
            <option value="">All severity</option>
            <option value="INFO">Information</option>
            <option value="WARNING">Warnings</option>
            <option value="ERROR">Errors</option>
          </select>
        }
      >
        <div className="filter-grid activity-filters">
          <label className="filter-field">
            Stage
            <select
              aria-label="Activity stage"
              value={stage}
              onChange={(event) => setStage(event.target.value)}
            >
              <option value="">All stages</option>
              {(snapshot.activity_stages || []).map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label className="filter-field">
            Event
            <select
              aria-label="Activity event"
              value={kind}
              onChange={(event) => setKind(event.target.value)}
            >
              <option value="">All events</option>
              {[
                ["DISCOVERED", "Discovered URL"],
                ["VALIDATED", "Website validated"],
                ["FOUND", "Found in Corpus"],
                ["MISSING", "Missing from Corpus"],
                ["RENDERING", "Browser rendering"],
                ["FILTER_DETECTED", "Filter detected"],
                ["PAGINATION_CHECKING", "Pagination probe"],
                ["CORPUS_BACKOFF", "Corpus recovery"],
                ["LIMITED", "Coverage boundary"],
                ["JAVASCRIPT_DIAGNOSTIC", "Website script note"],
              ].map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        </div>
        {error && (
          <p className="table-empty" role="alert">
            {error}
          </p>
        )}
        <div className="timeline">
          {events.map((event) => (
            <details
              className={`activity-event ${event.severity.toLowerCase()}`}
              key={event.id}
            >
              <summary>
                <span className="event-kind">
                  {event.severity} · {event.label}
                </span>
                <strong>{event.message}</strong>
                <time>{time(event.created_at)}</time>
              </summary>
              {event.url && <p className="event-url">{event.url}</p>}
              {event.issue && (
                <dl className="activity-explanation">
                  <dt>What happened</dt>
                  <dd>{event.issue}</dd>
                  <dt>Impact</dt>
                  <dd>{event.impact}</dd>
                  <dt>Continuation</dt>
                  <dd>{event.continued}</dd>
                  <dt>Next step</dt>
                  <dd>{event.next_step}</dd>
                </dl>
              )}
              <p className="footnote">
                Stage: {event.stage} · Depth: {event.depth ?? "Unknown"}
              </p>
              {Object.keys(event.details).length > 0 && (
                <pre className="technical-detail">
                  {JSON.stringify(event.details, null, 2)}
                </pre>
              )}
            </details>
          ))}
          {!events.length && (
            <p className="table-empty">No matching events recorded.</p>
          )}
        </div>
      </Panel>
    </>
  );
}
