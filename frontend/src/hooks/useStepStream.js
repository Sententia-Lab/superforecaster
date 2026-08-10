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
 * `active`, `failure` and `streaming` are three separate things on purpose.
 *
 * `active` is work in flight and nothing else — it is what draws the spinner and the
 * thought tail, so it has to end when the request does. It used to survive a failure so
 * the message stayed on screen, and that made a failed step render the *running* view
 * for ever: a spinner, no error text, and no Retry button, because the card only reaches
 * `StepControls` when `active` is null.
 *
 * `failure` is where the message goes instead. It outlives the request, so the card can
 * show what went wrong next to the button that retries it.
 *
 * `streaming` is the one buttons key off. Keying them off `active` left every Run and
 * Retry button disabled after any failure, with a page reload as the only way out.
 *
 * `start` resolves to the run the stream produced, or null if the step failed — which is
 * what lets `useRunQueue` chain one step into the next without a refetch.
 */
export function useStepStream({ onRun, onDone }) {
  const controllerRef = useRef(null);
  const [active, setActive] = useState(null);
  const [failure, setFailure] = useState(null);
  const [streaming, setStreaming] = useState(false);
  // active  = { stepId, thoughts: string, query: string, sources: [] }
  // failure = { stepId, message: string }

  const abort = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setActive(null);
    setFailure(null);
    setStreaming(false);
  }, []);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const start = useCallback(
    async (runId, stepId, { maxIterations } = {}) => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      setActive({ stepId, thoughts: "", query: "", sources: [] });
      // A new attempt clears the last one's message, including another step's: the
      // failed step keeps its own error on its row, so nothing is lost by dropping it
      // here, and two error banners for one run is one more than is true.
      setFailure(null);
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
              setFailure({ stepId, message: frame.payload.message });
            } else if (frame.type === "run") {
              latestRun = frame.payload;
              onRun?.(frame.payload);
            }
          },
        });
      } catch (e) {
        // The transport died rather than the step — the server may never have written
        // an error onto the row, so this message is the only account of what happened.
        if (e.name !== "AbortError") {
          setFailure({ stepId, message: e.message });
        }
      } finally {
        // Only the still-current controller may clear state — a superseded stream
        // finishing late must not switch off the one that replaced it.
        if (controllerRef.current === controller) {
          controllerRef.current = null;
          setActive(null);
          setStreaming(false);
          onDone?.();
        }
      }
      return latestRun;
    },
    [onRun, onDone],
  );

  return { active, failure, streaming, start, abort };
}
