import { useEffect, useState } from "react";
import { api } from "../services/api.js";

/** Local inventory polling and job actions; polling never triggers a crawl. */
export function useDashboard() {
  const [state, setState] = useState({
    runs: [],
    active: null,
    settings: { capture_types: ["HTML", "PDF", "DOCUMENT"] },
  });
  const [runId, setRunId] = useState(null);
  const [snapshot, setSnapshot] = useState(null);
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);
  const [connected, setConnected] = useState(false);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer;
    let controller;
    async function poll() {
      let delay = 5000;
      controller = new AbortController();
      try {
        const data = await api("/state", undefined, "GET", {
          signal: controller.signal,
        });
        if (cancelled) return;
        if (data.active) delay = 2000;
        setState(data);
        const selected = runId ?? data.active?.run_id ?? data.runs[0]?.id;
        if (selected && runId === null) {
          setRunId(selected);
          return;
        }
        if (selected) {
          const result = await api(
            `/runs/${selected}/summary`,
            undefined,
            "GET",
            {
              signal: controller.signal,
            },
          );
          if (cancelled) return;
          setSnapshot(result);
        }
        setConnected(true);
      } catch (failure) {
        if (!cancelled && failure.name !== "AbortError") {
          setConnected(false);
          setError(failure.message);
        }
      } finally {
        if (!cancelled) timer = setTimeout(poll, delay);
      }
    }
    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller?.abort();
    };
  }, [runId, refresh]);

  async function action(fn) {
    setWorking(true);
    setError("");
    try {
      await fn();
      setRefresh((value) => value + 1);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setWorking(false);
    }
  }

  return {
    state,
    setState,
    runId,
    setRunId,
    snapshot,
    setSnapshot,
    error,
    setError,
    working,
    connected,
    setRefresh,
    action,
  };
}
