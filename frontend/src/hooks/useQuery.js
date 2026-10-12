import { useEffect, useState } from "react";
import { api } from "../services/api.js";

/** Bounded, cancellable view reads. Heavy analysis is requested only on entry. */
export function useQuery(path, interval = 0, revision = 0) {
  const [result, setResult] = useState({
    data: null,
    error: "",
    loading: true,
  });
  useEffect(() => {
    let cancelled = false;
    let timer;
    const controller = new AbortController();
    setResult({ data: null, error: "", loading: true });
    async function read() {
      if (!path) return;
      try {
        const data = await api(path, undefined, "GET", {
          signal: controller.signal,
        });
        if (!cancelled) setResult({ data, error: "", loading: false });
      } catch (error) {
        if (!cancelled && error.name !== "AbortError")
          setResult((previous) => ({
            ...previous,
            error: error.message,
            loading: false,
          }));
      } finally {
        if (!cancelled && interval) timer = setTimeout(read, interval);
      }
    }
    read();
    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [path, interval, revision]);
  return result;
}
