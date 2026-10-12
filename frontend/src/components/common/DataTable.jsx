import { number } from "../../utils/format.js";

export function DataTable({
  rows,
  empty = "No data recorded in this scan.",
  limit = 200,
}) {
  if (!rows?.length) return <p className="table-empty">{empty}</p>;
  const keys = Object.keys(rows[0]);
  return (
    <div className="table-scroll">
      <table className="analysis-table">
        <thead>
          <tr>
            {keys.map((key) => (
              <th key={key}>{key}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, limit).map((row, i) => (
            <tr key={i}>
              {keys.map((key) => (
                <td key={key}>
                  {typeof row[key] === "boolean"
                    ? row[key]
                      ? "Yes"
                      : "No"
                    : typeof row[key] === "object" && row[key]
                      ? JSON.stringify(row[key])
                      : String(row[key] ?? "—")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > limit && (
        <p className="table-empty">
          Showing {limit} of {number(rows.length)} records. See Analysis or
          download the full report for more.
        </p>
      )}
    </div>
  );
}
