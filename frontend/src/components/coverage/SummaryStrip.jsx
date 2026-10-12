import { FileCode2, FileText, Files, Link2, AlertCircle } from "lucide-react";
import { number, site } from "../../utils/format.js";

export function SummaryStrip({ snapshot }) {
  const items = [
    ["HTML pages found", snapshot?.types.HTML.eligible, FileCode2],
    [
      "HTML missing from corpus",
      snapshot?.types.HTML.missing,
      AlertCircle,
      "gap",
    ],
    ["PDFs found", snapshot?.types.PDF.eligible, FileText],
    [
      "PDFs missing from corpus",
      snapshot?.types.PDF.missing,
      AlertCircle,
      "gap",
    ],
    ["Documents found", snapshot?.types.DOCUMENT.eligible, Files],
    ["Total URLs found", snapshot?.metrics.discovered, Link2],
  ];
  return (
    <section className="summary-strip" aria-label="Selected website analysis">
      <div className="summary-context">
        <span className="eyebrow">WEBSITE ANALYSIS</span>
        <strong>{site(snapshot?.run.target_url)}</strong>
        <span>
          {snapshot
            ? `Inventory #${snapshot.run.id} · Missing counts require a successful Corpus check`
            : "Start a scan or select a saved website to see its analysis"}
        </span>
      </div>
      <div className="summary-grid">
        {items.map(([label, value, Icon, tone]) => (
          <div className={`summary-item ${tone || ""}`} key={label}>
            <Icon size={17} aria-hidden="true" />
            <span>{label}</span>
            <strong>{number(value)}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}
