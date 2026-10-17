import { distinctPageTitle } from "../../utils/inventory.js";

export function UrlIdentityCell({ row, onDetail }) {
  const title = distinctPageTitle(row.title, row.url);
  return (
    <button
      className="url-button"
      title={row.url}
      aria-label={row.url}
      onClick={() => onDetail(row)}
    >
      <span className="url-address">{row.url}</span>
      {title && (
        <small className="url-title" title={title}>
          {title}
        </small>
      )}
    </button>
  );
}
