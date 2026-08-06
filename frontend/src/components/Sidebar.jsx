function RailItem({ run, selected, onSelect }) {
  return (
    <button
      className={`rail-item${selected ? " selected" : ""}`}
      onClick={() => onSelect(run.id)}
    >
      <span className="q">{run.question || "Untitled forecast"}</span>
      <span className="meta">
        {run.error ? <span className="chip red">error</span> : null}
        {run.status === "complete" ? <span className="chip green">done</span> : null}
        {run.status === "active" && !run.error ? (
          <span className="chip">in progress</span>
        ) : null}
      </span>
    </button>
  );
}

export default function Sidebar({ runs, selectedId, newSelected, onSelect, onNew }) {
  const backlog = runs.filter((r) => r.status === "backlog");
  const running = runs.filter((r) => r.status === "active");
  const complete = runs.filter((r) => r.status === "complete");

  const section = (title, items) => (
    <>
      <h3>{title}</h3>
      {items.length === 0 ? (
        <div className="empty">Nothing here.</div>
      ) : (
        items.map((r) => (
          <RailItem
            key={r.id}
            run={r}
            selected={r.id === selectedId}
            onSelect={onSelect}
          />
        ))
      )}
    </>
  );

  return (
    <nav className="rail">
      <button
        className={`btn new-forecast-btn${newSelected ? " primary" : ""}`}
        onClick={onNew}
      >
        + New forecast
      </button>
      {section("Running", running)}
      {section("Backlog", backlog)}
      {section("Complete", complete)}
    </nav>
  );
}
