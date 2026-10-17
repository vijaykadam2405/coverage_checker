import { textCoverageValue } from "../../utils/inventory.js";

export function TextCoverageCell({ row }) {
  const value = textCoverageValue(row);
  const method =
    row.text_coverage_method === "unique_word_coverage"
      ? "Estimated word coverage"
      : "Text similarity";
  if (value === null) {
    return (
      <span
        className="text-coverage-unavailable"
        title={row.text_coverage_label || "Text not compared"}
        aria-label={`Text coverage unavailable: ${row.text_coverage_label || "Text not compared"}`}
      >
        —
      </span>
    );
  }
  return (
    <div
      className="text-coverage-cell"
      title={`${method}: ${value.toFixed(1)}% · ${row.text_coverage_label || ""}`}
    >
      <span className="text-coverage-value">{value.toFixed(1)}%</span>
      <div
        className={`text-coverage-track ${value >= 90 ? "high" : value >= 60 ? "medium" : "low"}`}
        role="progressbar"
        aria-label="HTML text coverage"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={value}
        aria-valuetext={`${value.toFixed(1)}% ${method.toLowerCase()}`}
      >
        <span className="text-coverage-fill" style={{ width: `${value}%` }} />
      </div>
    </div>
  );
}
