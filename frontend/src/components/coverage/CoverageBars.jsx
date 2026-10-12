import { percentage } from "./Coverage.jsx";
export function CoverageBars({ rows, label }) {
  return (
    <div className="chart-bars">
      {rows.map((row, index) => {
        const total = row.Found + row.Missing + row.Unverified;
        return (
          <div className="chart-bar" key={index}>
            <span>{row[label]}</span>
            <div
              className="stacked-track"
              role="img"
              aria-label={`${row.Found} found, ${row.Missing} missing, ${row.Unverified} unverified`}
            >
              <i
                className="found"
                style={{ width: `${total ? (100 * row.Found) / total : 0}%` }}
              />
              <i
                className="missing"
                style={{ width: `${total ? (100 * row.Missing) / total : 0}%` }}
              />
              <i
                className="pending"
                style={{
                  width: `${total ? (100 * row.Unverified) / total : 0}%`,
                }}
              />
            </div>
            <b>{percentage(row["Coverage %"])}</b>
          </div>
        );
      })}
      <p className="chart-key">
        Found · Missing · Unverified — percentage uses verified results only.
      </p>
    </div>
  );
}
