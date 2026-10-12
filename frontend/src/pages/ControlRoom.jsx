import { useState } from "react";
import { Panel } from "../components/common/Panel.jsx";
import { TYPES } from "../config/capture.js";
import { SOURCES, NUMERIC_CONTROLS } from "../config/controls.js";

const GROUPS = {
  capture: "Capture",
  discovery: "Discovery",
  scope: "Scope",
  browser: "Browser",
  pagination: "Pagination & filters",
  performance: "Performance",
  corpus: "Corpus",
};
export function ControlRoom({
  value,
  active,
  save,
  corpus,
  target,
  sessionAvailable,
}) {
  const [settings, setSettings] = useState(value);
  const [group, setGroup] = useState("capture");
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState("");
  const disabled = !!active || working;
  function update(name, value) {
    setSettings((previous) => ({ ...previous, [name]: value }));
    setMessage("");
  }
  function toggleList(name, value, checked) {
    update(
      name,
      checked
        ? [...settings[name], value]
        : settings[name].filter((item) => item !== value),
    );
  }
  const toggle = (name, label, help) => (
    <label className="capture-option" key={name}>
      <span>
        <strong>{label}</strong>
        <small>{help}</small>
      </span>
      <input
        type="checkbox"
        role="switch"
        aria-label={label}
        checked={!!settings[name]}
        disabled={disabled}
        onChange={(event) => update(name, event.target.checked)}
      />
    </label>
  );
  return (
    <Panel
      title="Capture control room"
      subtitle="Saved settings apply to the next discovery; existing inventories retain their original scope"
    >
      <div
        className="inventory-types"
        role="tablist"
        aria-label="Control room groups"
      >
        {Object.entries(GROUPS).map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={group === key}
            aria-controls={`control-${key}`}
            tabIndex={group === key ? 0 : -1}
            onKeyDown={(event) => {
              const keys = Object.keys(GROUPS);
              const index = keys.indexOf(key);
              const next =
                event.key === "ArrowRight"
                  ? keys[(index + 1) % keys.length]
                  : event.key === "ArrowLeft"
                    ? keys[(index + keys.length - 1) % keys.length]
                    : event.key === "Home"
                      ? keys[0]
                      : event.key === "End"
                        ? keys[keys.length - 1]
                        : null;
              if (next) {
                event.preventDefault();
                setGroup(next);
                event.currentTarget.parentElement
                  .querySelectorAll("[role=tab]")
                  [keys.indexOf(next)].focus();
              }
            }}
            className={group === key ? "selected" : ""}
            onClick={() => setGroup(key)}
          >
            {label}
          </button>
        ))}
      </div>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setWorking(true);
          setMessage("");
          try {
            await save(settings);
            setMessage("Settings saved for your next discovery.");
          } catch (error) {
            setMessage(error.message);
          } finally {
            setWorking(false);
          }
        }}
      >
        <div
          className="control-grid"
          role="tabpanel"
          id={`control-${group}`}
          aria-label={GROUPS[group]}
        >
          {group === "capture" &&
            Object.entries(TYPES).map(([kind, label]) => (
              <label className="capture-option" key={kind}>
                <span>
                  <strong>{label}</strong>
                  <small>
                    {kind === "HTML"
                      ? "Validate HTML and automatically compare live page text with Corpus text."
                      : ["API", "FEED"].includes(kind)
                        ? "Collect endpoint URLs; metadata can be traversed to discover HTML links."
                        : "URL only. No file download, text extraction or availability test."}
                  </small>
                </span>
                <input
                  type="checkbox"
                  role="switch"
                  aria-label={`Capture ${label}`}
                  checked={settings.capture_types.includes(kind)}
                  disabled={disabled}
                  onChange={(event) =>
                    toggleList("capture_types", kind, event.target.checked)
                  }
                />
              </label>
            ))}
          {group === "discovery" &&
            Object.entries(SOURCES).map(([key, [label, help]]) => (
              <label className="capture-option" key={key}>
                <span>
                  <strong>{label}</strong>
                  <small>{help}</small>
                </span>
                <input
                  type="checkbox"
                  role="switch"
                  aria-label={label}
                  checked={settings.discovery_sources.includes(key)}
                  disabled={disabled}
                  onChange={(event) =>
                    toggleList("discovery_sources", key, event.target.checked)
                  }
                />
              </label>
            ))}
          {group === "scope" && (
            <>
              {toggle(
                "include_subdomains",
                "Linked subdomains",
                "Include discovered subdomains; this does not enumerate unlinked DNS hosts.",
              )}
              {toggle(
                "external_resources",
                "External resource links",
                "Collect selected document/image/asset URLs on linked external hosts.",
              )}
              <div className="control-note">
                <strong>
                  Selected website: {target || "No inventory selected"}
                </strong>
                <p>
                  The inventory safeguard below bounds work, not navigation
                  depth.
                </p>
                <strong>Robots policy: ON</strong>
                <p>
                  Downloaded robots restrictions and Crawl-Delay are honored. No
                  depth cutoff is used. Private-network and metadata targets are
                  blocked in normal operation.
                </p>
              </div>
            </>
          )}
          {group === "browser" && (
            <>
              {toggle(
                "use_browser",
                "Enable browser discovery",
                "Requires Rendered pages in Discovery. Uses normal Chromium, without stealth or access-control evasion.",
              )}
              {toggle(
                "reuse_public_session",
                "Reuse prepared public session",
                "Use locally prepared public-page cookies when available; cookie values never appear here.",
              )}
              <div className="control-note">
                <p>
                  Prepared public session:{" "}
                  {target
                    ? sessionAvailable
                      ? "Local session file available; freshness is checked when used."
                      : "Not prepared for this website."
                    : "Select a website to see availability."}
                </p>
                <p>
                  Public pages only. CAPTCHA/login walls remain boundaries.
                  Manual public challenge preparation is documented in README;
                  no automated login or CAPTCHA solving.
                </p>
              </div>
            </>
          )}
          {group === "pagination" && (
            <div className="control-note">
              <strong>Observed cursor pagination</strong>
              <p>
                Follow site-supplied cursors/offset links; never invent opaque
                values. Numeric exponential/binary probes require a monotonic
                series; blocking, repetition or safeguards leave the maximum
                unknown.
              </p>
            </div>
          )}
          {group === "corpus" && (
            <div className="control-note">
              <strong>
                Read-only Coverage API ·{" "}
                {corpus?.configured ? "Key configured" : "Key not configured"}
              </strong>
              <p>
                API-only verification. No dashboard/browser fallback, no file
                text. Each URL is checked automatically during the scan, before
                moving to the next URL. One URL per request; at least 3 seconds
                between Corpus requests, with additional backoff under load.
              </p>
            </div>
          )}
          {(NUMERIC_CONTROLS[group] || []).map(
            ([key, label, min, max, step, help]) => (
              <label className="numeric-control" key={key}>
                <strong>{label}</strong>
                <input
                  type="number"
                  aria-label={label}
                  min={min}
                  max={max}
                  step={step}
                  required
                  value={settings[key]}
                  disabled={disabled}
                  onChange={(event) =>
                    update(
                      key,
                      event.target.value === ""
                        ? ""
                        : Number(event.target.value),
                    )
                  }
                />
                <small>{help}</small>
                <small>
                  Allowed: {min}–{max}
                </small>
              </label>
            ),
          )}
        </div>
        <p className="footnote">
          HTML/API metadata may be visited to discover enabled file types when
          their own capture is off. Traversal-only URLs are excluded from
          inventory, reports and Corpus checks.
        </p>
        {active && (
          <p className="warning-text">
            Settings are locked while a scan is active.
          </p>
        )}
        {message && (
          <p className="settings-message" role="status">
            {message}
          </p>
        )}
        <div className="control-footer">
          <span>
            {settings.capture_types.length} capture types ·{" "}
            {settings.discovery_sources.length} discovery sources
          </span>
          <button
            type="submit"
            className="primary"
            disabled={disabled || !settings.capture_types.length}
          >
            {working ? "Saving…" : "Save settings"}
          </button>
        </div>
      </form>
    </Panel>
  );
}
