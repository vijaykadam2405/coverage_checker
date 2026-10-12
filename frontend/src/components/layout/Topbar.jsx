import { ChevronRight, Menu } from "lucide-react";
import { NAVIGATION } from "../../config/navigation.js";

export function Topbar({ view, menu, setMenu }) {
  return (
    <header className="topbar">
      <div>
        <button
          className="icon-button mobile-menu"
          aria-label="Open navigation"
          onClick={() => setMenu(!menu)}
        >
          <Menu size={22} />
        </button>
        <span className="breadcrumb">
          Workspace
          <ChevronRight size={14} />
          <b>{NAVIGATION.find((item) => item[0] === view)?.[1]}</b>
        </span>
      </div>
      <span className="topbar-tag">
        <span className="status-dot online" /> PUBLIC WEBSITE AUDIT
      </span>
    </header>
  );
}
