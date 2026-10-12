import { Panel } from "../common/Panel.jsx";
import { number } from "../../utils/format.js";
import { TYPES } from "../../config/capture.js";

export const percentage = (value) =>
  value == null ? "N/A" : `${value.toFixed(1)}%`;
export function Coverage({ snapshot }) {
  const {
    eligible,
    found,
    missing,
    unverified,
    checked,
    verified_coverage,
    verification_completion,
    coverage_lower_bound,
    coverage_upper_bound,
  } = snapshot.html_coverage;
  const gradient = eligible
    ? `conic-gradient(var(--green) 0 ${(100 * found) / eligible}%, var(--red) 0 ${(100 * checked) / eligible}%, var(--track) 0 100%)`
    : "var(--track)";
  return (
    <Panel
      title="Corpus coverage analysis"
      subtitle="HTML pages and every collected resource type · found, missing and awaiting verification"
    >
      <div className="coverage-body">
        <div
          className="donut"
          style={{ background: gradient }}
          role="img"
          aria-label={`${found} found, ${missing} missing, ${unverified} unverified HTML pages`}
        >
          <div>
            <strong>{percentage(verified_coverage)}</strong>
            <span>HTML URL coverage</span>
          </div>
        </div>
        <div className="legend">
          {[
            ["found", "HTML found in corpus", found],
            ["missing", "HTML missing from corpus", missing],
            ["pending", "HTML not yet verified", unverified],
          ].map(([kind, label, count]) => (
            <div key={kind}>
              <i className={kind} />
              <span>{label}</span>
              <strong>{number(count)}</strong>
            </div>
          ))}
          <p>
            Verification completion:{" "}
            <b>{percentage(verification_completion)}</b> ({checked}/{eligible}).
            {unverified > 0 && (
              <>
                {" "}
                Coverage bounds: {percentage(coverage_lower_bound)}–
                {percentage(coverage_upper_bound)}. The final result is not
                established.
              </>
            )}
          </p>
        </div>
      </div>
      <div className="table-scroll">
        <table className="coverage-type-table">
          <thead>
            <tr>
              <th>Content type</th>
              <th>Found by CrawlScope</th>
              <th>Found in corpus</th>
              <th>Missing from corpus</th>
              <th>Not verified</th>
              <th>Corpus coverage</th>
              <th>Checks complete</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(TYPES)
              .filter(
                ([kind]) =>
                  ["HTML", "PDF", "DOCUMENT"].includes(kind) ||
                  snapshot.types[kind].discovered > 0,
              )
              .map(([kind, label]) => {
                const item = snapshot.types[kind];
                return (
                  <tr key={kind}>
                    <td>{label}</td>
                    <td>{number(item.eligible)}</td>
                    <td>{number(item.found)}</td>
                    <td className={item.missing ? "missing-count" : ""}>
                      {number(item.missing)}
                    </td>
                    <td>{number(item.unverified)}</td>
                    <td>
                      <div className="coverage-number">
                        {percentage(item.verified_coverage)}
                      </div>
                      <div
                        className="stacked-track"
                        role="img"
                        aria-label={`${label}: ${item.found} found, ${item.missing} missing, ${item.unverified} not verified`}
                      >
                        {[
                          ["found", item.found],
                          ["missing", item.missing],
                          ["pending", item.unverified],
                        ].map(([state, count]) => (
                          <i
                            key={state}
                            className={state}
                            style={{
                              width: `${item.eligible ? (100 * count) / item.eligible : 0}%`,
                            }}
                          />
                        ))}
                      </div>
                    </td>
                    <td>
                      {item.not_tracked
                        ? `${number(item.not_tracked)} not tracked by corpus`
                        : percentage(item.verification_completion)}
                    </td>
                  </tr>
                );
              })}
          </tbody>
          <tfoot>
            <tr>
              <th>All collected types</th>
              <td>{number(snapshot.metrics.eligible)}</td>
              <td>{number(snapshot.metrics.found)}</td>
              <td>{number(snapshot.metrics.missing)}</td>
              <td>{number(snapshot.metrics.unverified)}</td>
              <td>{percentage(snapshot.metrics.verified_coverage)}</td>
              <td>{percentage(snapshot.metrics.verification_completion)}</td>
            </tr>
          </tfoot>
        </table>
      </div>
      <p className="footnote">
        {number(snapshot.metrics.failed)} failed/excluded candidates and{" "}
        {number(snapshot.metrics.pending_website)} URLs awaiting website
        validation are outside this coverage table.{" "}
        {number(snapshot.counters.boundaries)} exploration boundaries are
        recorded in Activity; coverage applies only to the observed inventory.{" "}
        Corpus coverage = found ÷ (found + missing), calculated separately for
        each type. Unchecked URLs and failed candidates are not classified as
        missing. Files are URL-only, not availability or text tests. HTML URL
        presence does not prove text completeness. Missing resources are crawler
        gaps only if those types are in the production crawler’s scope.
        {snapshot.metrics.not_tracked > 0 &&
          ` ${number(snapshot.metrics.not_tracked)} resource URLs are not tracked by Corpus and are excluded from verification completion and coverage bounds, not counted as misses.`}
      </p>
    </Panel>
  );
}
