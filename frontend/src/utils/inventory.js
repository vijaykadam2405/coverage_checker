// Presentation helpers only. Never infer a coverage score from Corpus presence.
export function availableRunId(runs, selected, active) {
  if (runs.some((run) => run.id === selected)) return selected;
  if (runs.some((run) => run.id === active)) return active;
  return runs[0]?.id ?? null;
}

export function textCoverageValue(row) {
  const value = row.text_coverage_percent;
  return typeof value === "number" &&
    Number.isFinite(value) &&
    value >= 0 &&
    value <= 100
    ? value
    : null;
}

export function distinctPageTitle(title, url) {
  const value = typeof title === "string" ? title.trim() : "";
  if (!value) return "";
  const comparable = (text) => text.trim().replace(/\/$/, "").toLowerCase();
  if (comparable(value) === comparable(url || "")) return "";
  try {
    const titleUrl = new URL(value);
    const pageUrl = new URL(url);
    if (
      titleUrl.hostname.toLowerCase() === pageUrl.hostname.toLowerCase() &&
      titleUrl.port === pageUrl.port &&
      comparable(decodeURI(titleUrl.pathname)) ===
        comparable(decodeURI(pageUrl.pathname)) &&
      titleUrl.search === pageUrl.search
    )
      return "";
  } catch {
    // A normal page title is not a URL.
  }
  return value;
}
