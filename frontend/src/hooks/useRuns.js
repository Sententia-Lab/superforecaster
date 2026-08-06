import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

/** The sidebar's list of runs, refetched on demand after any step completes. */
export function useRuns() {
  const [runs, setRuns] = useState([]);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      setRuns(await api.listRuns());
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { runs, refresh, error };
}
