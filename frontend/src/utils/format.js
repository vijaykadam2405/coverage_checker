export const number = (value) => new Intl.NumberFormat("en").format(value ?? 0);

export const time = (value) =>
  value
    ? new Date(value).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";

export function site(value) {
  try {
    return new URL(value).hostname;
  } catch {
    return value || "No website selected";
  }
}

export function period(value, now = Date.now()) {
  if (!value) return "—";
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "—";
  return `${Math.floor(Math.max(0, now - timestamp) / 86400000)}d ago`;
}
