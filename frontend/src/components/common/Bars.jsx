import { number } from "../../utils/format.js";
export function Bars({ items, label = "label", value = "count", unit = "" }) {
  const maximum =
    unit === "%"
      ? 100
      : Math.max(1, ...items.map((item) => Number(item[value]) || 0));
  return (
    <div className="chart-bars" role="list">
      {items.map((item, index) => (
        <div className="chart-bar" key={index} role="listitem">
          <span>{String(item[label] ?? "Unknown")}</span>
          <div className="bar-track" aria-hidden="true">
            <i style={{ width: `${(100 * (item[value] || 0)) / maximum}%` }} />
          </div>
          <b>{item[value] === null ? "N/A" : number(item[value]) + unit}</b>
        </div>
      ))}
    </div>
  );
}
