import { useCallback, useRef, useState } from "react";
import { nextRunnable } from "../runQueue.js";

/**
 * Run every remaining step, one at a time, from one click.
 *
 * There is no server-side queue and no background job. The request *is* the step
 * (ADR 46), and its last frame is the updated run — so "run everything" is a loop in the
 * browser that feeds each response into the next round. Nothing survives closing the tab,
 * which is the same promise the single-step path already makes.
 *
 * A failure stops the loop: the stream emits an error frame and no run frame, so `start`
 * resolves to null and there is nothing to continue from. One click is one honest attempt
 * at everything remaining — no hidden retries, and no way to spin on a step that always
 * fails. Clicking Run All again picks the failed step back up.
 */
export function useRunQueue({ stream }) {
  const [scope, setScope] = useState(null); // null | "all" | <stage name>
  const cancelled = useRef(false);

  // `stream.start` is memoized on its callbacks, and RunView passes inline arrows, so its
  // identity changes every render. Holding it in a ref keeps `drain` stable — otherwise a
  // re-render mid-run would rebuild the loop it is currently inside.
  const startRef = useRef(stream.start);
  startRef.current = stream.start;

  const drain = useCallback(async (nextScope, startRun) => {
    setScope(nextScope);
    cancelled.current = false;
    let tree = startRun;
    try {
      while (!cancelled.current) {
        const next = nextRunnable(tree, nextScope);
        if (!next) break;
        const after = await startRef.current(tree.id, next.id);
        if (!after) break; // failed, cancelled, or stopped — the error card says which
        tree = after;
      }
    } finally {
      setScope(null);
    }
  }, []);

  const stop = useCallback(() => {
    cancelled.current = true;
    // Disconnecting cancels the in-flight step server-side, which lands it as
    // `error='cancelled'` and immediately claimable again (ADR 46).
    stream.abort();
  }, [stream]);

  return { scope, running: scope !== null, drain, stop };
}
