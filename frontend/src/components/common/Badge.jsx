import { statusLabels } from "../../config/status.js";

export function Badge({ status, children }) {
  return (
    <span
      className={`badge ${{ checking: "processing", fetching: "processing", rendering: "processing", unverified: "warning", validated: "found", not_tracked: "pending" }[status] || status || ""}`}
    >
      <span className="status-dot" />
      {children || statusLabels[status] || status}
    </span>
  );
}
