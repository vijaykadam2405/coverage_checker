import { useEffect, useRef, useState } from "react";
import { KeyRound, ShieldCheck } from "lucide-react";
import { Panel } from "../components/common/Panel.jsx";
import { CAPTURE_OPTIONS } from "../config/controls.js";

export function ControlRoom({ value, active, save, corpus, saveKey }) {
  const [settings, setSettings] = useState(value);
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState("");
  const [key, setKey] = useState("");
  const [keyWorking, setKeyWorking] = useState(false);
  const [keyMessage, setKeyMessage] = useState("");
  const [keyError, setKeyError] = useState(false);
  const savedSettings = useRef(value);
  useEffect(() => {
    if (JSON.stringify(value) !== JSON.stringify(savedSettings.current)) {
      savedSettings.current = value;
      setSettings(value);
    }
  }, [value]);
  const disabled = !!active || working || keyWorking;
  const environmentKey = corpus?.source === "environment";
  const keySupported = corpus?.key_management_supported === true;

  function toggleCapture(types, checked) {
    setSettings((previous) => ({
      ...previous,
      capture_types: checked
        ? [...new Set([...previous.capture_types, ...types])]
        : previous.capture_types.filter((kind) => !types.includes(kind)),
    }));
    setMessage("");
  }

  return (
    <>
      <Panel
        title="Capture control room"
        subtitle="Choose what to collect. Discovery and Corpus verification run automatically."
      >
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            setWorking(true);
            setMessage("");
            try {
              await save(settings);
              setMessage("Capture choices saved for your next scan.");
            } catch (error) {
              setMessage(error.message);
            } finally {
              setWorking(false);
            }
          }}
        >
          <div className="control-grid">
            {CAPTURE_OPTIONS.map(({ label, types, help }) => (
              <label className="capture-option" key={label}>
                <span>
                  <strong>{label}</strong>
                  <small>{help}</small>
                </span>
                <input
                  type="checkbox"
                  role="switch"
                  aria-label={`Capture ${label}`}
                  checked={types.some((kind) =>
                    settings.capture_types.includes(kind),
                  )}
                  disabled={disabled}
                  onChange={(event) =>
                    toggleCapture(types, event.target.checked)
                  }
                />
              </label>
            ))}
          </div>
          <div className="automatic-discovery-note">
            <ShieldCheck size={19} aria-hidden="true" />
            <div>
              <strong>Discovery runs automatically</strong>
              <p>
                Page links, sitemaps, JavaScript, public APIs, filters, tabs,
                pagination, archives and Wayback are checked without separate
                switches. Historical HTML URLs must pass live website validation
                before they count toward coverage.
              </p>
              <p>
                Corpus checks stay read-only and one URL at a time, with
                automatic slowdown under load. Only HTML text is compared; files
                remain URL-only.
              </p>
            </div>
          </div>
          {active && (
            <p className="warning-text">
              Pause the scan and wait for it to save before changing settings or
              the API key.
            </p>
          )}
          {message && (
            <p className="settings-message" role="status">
              {message}
            </p>
          )}
          <div className="control-footer">
            <span>Changes apply to new scans; saved results are retained.</span>
            <button
              type="submit"
              className="primary"
              disabled={disabled || !settings.capture_types.length}
            >
              {working ? "Saving…" : "Save capture choices"}
            </button>
          </div>
        </form>
      </Panel>
      <Panel
        title="Corpus API connection"
        subtitle="The only connection to your production crawler's data. API access only—no Corpus website login."
      >
        <div className="corpus-key-status">
          <KeyRound size={18} aria-hidden="true" />
          <strong>
            {corpus?.configured ? "API key configured" : "API key required"}
          </strong>
          <span>
            {environmentKey
              ? "Managed by backend environment"
              : "Protected on this computer"}
          </span>
        </div>
        <form
          className="corpus-key-form"
          onSubmit={async (event) => {
            event.preventDefault();
            if (disabled || environmentKey || !keySupported) return;
            setKeyWorking(true);
            setKeyMessage("");
            setKeyError(false);
            try {
              await saveKey(key.trim());
              setKeyMessage(
                "API key saved securely. Authentication is checked when your scan contacts Corpus.",
              );
            } catch (error) {
              setKeyError(true);
              setKeyMessage(error.message);
            } finally {
              setKey("");
              setKeyWorking(false);
            }
          }}
        >
          <label className="corpus-key-field" htmlFor="corpus-api-key">
            <span>
              {corpus?.configured ? "Replace API key" : "Corpus API key"}
            </span>
            <input
              id="corpus-api-key"
              type="password"
              autoComplete="new-password"
              spellCheck={false}
              maxLength={512}
              required
              placeholder="Paste your Corpus API key"
              value={key}
              onChange={(event) => {
                setKey(event.target.value);
                setKeyMessage("");
              }}
              disabled={disabled || environmentKey || !keySupported}
            />
          </label>
          <button
            type="submit"
            className="primary"
            disabled={
              disabled || environmentKey || !keySupported || !key.trim()
            }
          >
            {keyWorking ? "Saving key…" : "Save API key"}
          </button>
        </form>
        <p className="corpus-key-help">
          Existing keys are never displayed. On Windows, the key is encrypted
          for your user account—not saved in frontend storage, scan settings or
          reports.
        </p>
        {environmentKey && (
          <p className="info-box">
            Your environment key takes priority. Remove
            CRAWLSCOPE_CORPUS_API_KEY from the backend environment and restart
            before replacing it here.
          </p>
        )}
        {!keySupported && (
          <p className="info-box">
            Restart the updated Windows backend to enable this form. On other
            systems, set the key in the backend environment.
          </p>
        )}
        {keyMessage && (
          <p
            className={`settings-message ${keyError ? "warning-text" : ""}`}
            role={keyError ? "alert" : "status"}
          >
            {keyMessage}
          </p>
        )}
      </Panel>
    </>
  );
}
