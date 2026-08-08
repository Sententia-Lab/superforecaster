import { useCallback, useEffect, useRef, useState } from "react";
import { streamStep } from "../api.js";

/**
 * Drive one gated step's stream at a time.
 *
 * The AbortController is tied to component lifetime: unmounting (or starting a
 * different step) aborts the fetch, which disconnects, which cancels the step
 * server-side (ADR 46). State exposes what the active card renders: the step id,
 * the accumulated thought tail, the query line, and the source chips.
 *
 * `active` and `streaming` are separate on purpose. `active` survives a failure so the
 * error card stays on screen; `streaming` is true only while a request is in flight.
 * Buttons must key off `streaming` — keying them off `active` left every Run and Retry
 * button disabled after any failure, with a page reload as the only way out.
 *
 * `start` resolves to the run the stream produced, or null if the step failed — which is
 * what lets `useRunQueue` chain one step into the next without a refetch.
 */
export function useStepStream({ onRun, onDone }) {
  const controllerRef = useRef(null);
  const [active, setActive] = useState(null);
  const [streaming, setStreaming] = useState(false);
  // active = { stepId, thoughts: string, query: string, sources: [], error: string }

  const abort = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setActive(null);
    setStreaming(false);
  }, []);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const start = useCallback(
    async (runId, stepId, { maxIterations } = {}) => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      setActive({ stepId, thoughts: "", query: "", sources: [], error: "" });
      setStreaming(true);

      const patch = (fn) =>
        setActive((a) => (a && a.stepId === stepId ? fn(a) : a));

      let latestRun = null;

      try {
        await streamStep(runId, stepId, {
          signal: controller.signal,
          maxIterations,
          onEvent: (frame) => {
            if (frame.type === "thought") {
              patch((a) => ({
                ...a,
                thoughts: (a.thoughts + frame.payload.delta).slice(-4000),
              }));
            } else if (frame.type === "query") {
              patch((a) => ({
                ...a,
                query: `${frame.payload.tool}: ${frame.payload.q || ""}`,
              }));
            } else if (frame.type === "source") {
              patch((a) => ({ ...a, sources: [...a.sources, frame.payload] }));
            } else if (frame.type === "exhausted") {
              patch((a) => ({ ...a, query: "search budget exhausted — wrapping up" }));
            } else if (frame.type === "error") {
              patch((a) => ({ ...a, error: frame.payload.message }));
            } else if (frame.type === "run") {
              latestRun = frame.payload;
              onRun?.(frame.payload);
            }
          },
        });
      } catch (e) {
        if (e.name !== "AbortError") {
          patch((a) => ({ ...a, error: e.message }));
        }
      } finally {
        // Only the still-current controller may clear state — a superseded stream
        // finishing late must not switch off the one that replaced it.
        if (controllerRef.current === controller) {
          controllerRef.current = null;
          setActive((a) => (a && a.stepId === stepId && a.error ? a : null));
          setStreaming(false);
          onDone?.();
        }
      }
      return latestRun;
    },
    [onRun, onDone],
  );

  return { active, streaming, start, abort };
}
