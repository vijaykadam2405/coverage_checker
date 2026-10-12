import { ExternalLink } from "lucide-react";
import { TYPES } from "../../config/capture.js";
import { Dialog } from "../common/Dialog.jsx";
import { Badge } from "../common/Badge.jsx";

export function UrlEvidenceDialog({ detail, onClose }) {
  return (
    <Dialog title="URL evidence" onClose={onClose}>
      <div className="detail-title">
        <Badge status={detail.status} />
        <span className="muted">{TYPES[detail.category]}</span>
      </div>
      <a
        className="detail-url"
        href={detail.url}
        target="_blank"
        rel="noreferrer"
      >
        {detail.url}
        <ExternalLink size={16} />
      </a>
      <dl className="detail-grid">
        {Object.entries(detail.detail)
          .filter(([key]) => key !== "URL")
          .map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>
                {Array.isArray(value) ? value.join(", ") : String(value ?? "—")}
              </dd>
            </div>
          ))}
      </dl>
      {detail.provider?.startsWith("coverage_api:") && (
        <p className="info-box">
          Verified through the Corpus Coverage API. HTML text comparisons use
          the saved website snapshot and stored corpus text. Word coverage is an
          estimate, not proof of exact semantic completeness; capture dates may
          differ.
        </p>
      )}
      {detail.category !== "HTML" && (
        <p className="info-box">
          This is a URL-only resource. No file download, text extraction or
          content comparison was performed.
        </p>
      )}
    </Dialog>
  );
}
