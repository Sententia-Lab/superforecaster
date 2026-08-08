import { nextRunnable } from "../runQueue.js";

/**
 * The run's header: what was asked, and the one button that runs the rest of it.
 *
 * Only the title row sticks. Resolution criteria run to a full paragraph on a real
 * question, and a sticky block that tall covers the work it is supposed to sit above —
 * so the criteria scroll away and the bar that carries Run All does not.
 *
 * Run All is a browser loop, not a server job — thirty sequential agent calls with the
 * tab open, because one step runs at a time process-wide on purpose. Stop disconnects,
 * which cancels the step that is in flight and leaves it immediately re-runnable.
 */
export default function RunHeader({ run, queue, busy, onRunAll, onDelete }) {
  const complete = run.status === "complete";
  const hasWork = run.status === "backlog" || nextRunnable(run, "all") !== null;

  return (
    <>
      <div className="run-header">
        <h1 className="qtitle">{run.question}</h1>
        {complete && <span className="chip green">complete</span>}
        <div className="run-header-actions">
          {!complete && hasWork && queue.running && (
            <button className="btn danger" onClick={queue.stop}>
              Stop
            </button>
          )}
          {!complete && hasWork && !queue.running && (
            <button className="btn primary" disabled={busy} onClick={onRunAll}>
              Run All
            </button>
          )}
          <button
            className="btn tiny ghost"
            disabled={busy || queue.running}
            onClick={onDelete}
          >
            Delete
          </button>
        </div>
      </div>

      <div className="qmeta">
        Resolves <b>{(run.resolution_date || "").slice(0, 10)}</b> via{" "}
        <b>{run.resolution_source}</b> — {run.resolution_criteria}
      </div>
    </>
  );
}
