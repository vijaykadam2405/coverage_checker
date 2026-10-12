export function TextCoverageCell({ row }) {
  return (
    <span className="text-coverage-cell">
      {row.text_coverage_percent != null
        ? `${row.text_coverage_percent.toFixed(1)}%`
        : row.text_coverage_label || "Not compared"}
      {row.text_coverage_percent != null && (
        <small className="cell-note">
          {row.text_coverage_method === "unique_word_coverage"
            ? "Estimated word coverage"
            : "Text similarity"}{" "}
          · {row.text_coverage_label}
        </small>
      )}
    </span>
  );
}
