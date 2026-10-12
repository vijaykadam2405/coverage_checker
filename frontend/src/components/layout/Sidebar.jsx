import { Radar } from "lucide-react";
import { NAVIGATION } from "../../config/navigation.js";
import { useEffect, useRef, useState } from "react";

export function Sidebar({ view, setView, snapshot, connected, menu, setMenu }) {
  const ref = useRef(null);
  const [mobile, setMobile] = useState(
    () => window.matchMedia("(max-width: 800px)").matches,
  );
  useEffect(() => {
    const media = window.matchMedia("(max-width: 800px)");
    const update = () => setMobile(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    if (!menu || !mobile) return;
    const previous = document.activeElement;
    const controls = ref.current.querySelectorAll("a,button");
    controls[0]?.focus();
    const key = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setMenu(false);
      }
      if (event.key === "Tab") {
        const first = controls[0],
          last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      previous?.focus();
    };
  }, [menu, mobile, setMenu]);
  return (
    <>
      <aside
        ref={ref}
        inert={mobile && !menu}
        aria-hidden={mobile && !menu}
        className={`sidebar ${menu ? "open" : ""}`}
      >
        <a className="brand" href="/" aria-label="CrawlScope home">
          <span>
            <Radar size={25} />
          </span>
          CrawlScope<small>COVERAGE MONITOR</small>
        </a>
        <div className="nav-label">WORKSPACE</div>
        <nav>
          {NAVIGATION.map(([key, label, Icon]) => (
            <button
              key={key}
              aria-current={view === key ? "page" : undefined}
              className={view === key ? "active" : ""}
              onClick={() => {
                setView(key);
                setMenu(false);
              }}
            >
              <Icon size={18} />
              {label}
              {key === "activity" && !!snapshot?.boundaries.length && (
                <span>{snapshot.boundaries.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-status">
            <span className={`status-dot ${connected ? "online" : ""}`} />
            {connected
              ? "Local service connected"
              : "Connecting to local service"}
          </div>
          <p>
            Independent discovery.
            <br />
            Evidence-based coverage.
          </p>
        </div>
      </aside>
      {menu && (
        <button
          aria-label="Close navigation"
          className="mobile-scrim"
          onClick={() => setMenu(false)}
        />
      )}
    </>
  );
}
