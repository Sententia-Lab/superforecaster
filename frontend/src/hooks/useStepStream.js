import { useCallback, useEffect, useRef, useState } from "react";
import { streamStep } from "../api.js";

/**
 * Drive one gated step's stream at a time.
 *
 * The AbortController is tied to component lifetime: unmounting (or starting a
 * different step) aborts the fetch, which disconnects, which cancels the step
 * server-side (ADR 46). State exposes what the active card renders: the step id,
 * the accumulated thought tail, the query line, and the source chips.
 */
export function useStepStream({ onRun, onDone }) {
  const controllerRef = useRef(null);
  const [active, setActive] = useState(null);
  // active = { stepId, thoughts: string, query: string, sources: [], error: string }

  const abort = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setActive(null);
  }, []);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const start = useCallback(
    async (runId, stepId, { maxIterations } = {}) => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      setActive({ stepId, thoughts: "", query: "", sources: [], error: "" });

      const patch = (fn) =>
        setActive((a) => (a && a.stepId === stepId ? fn(a) : a));

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
              onRun?.(frame.payload);
            }
          },
        });
      } catch (e) {
        if (e.name !== "AbortError") {
          patch((a) => ({ ...a, error: e.message }));
        }
      } finally {
        if (controllerRef.current === controller) {
          controllerRef.current = null;
          setActive((a) => (a && a.stepId === stepId && a.error ? a : null));
          onDone?.();
        }
      }
    },
    [onRun, onDone],
  );

  return { active, start, abort };
}
