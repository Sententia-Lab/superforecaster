import { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";
import { useRuns } from "./hooks/useRuns.js";
import KeyPanel from "./components/KeyPanel.jsx";
import ResearchPanel from "./components/ResearchPanel.jsx";
import Sidebar from "./components/Sidebar.jsx";
import NewForecastView from "./components/NewForecastView.jsx";
import BacklogView from "./components/BacklogView.jsx";
import RunView from "./components/RunView.jsx";

const THEME_KEY = "sf_theme";

/** A page under a lens: what the run read, and the fact that it was looked at. */
function ResearchIcon() {
  return (
    <svg viewBox="0 0 20 20" width="15" height="15" aria-hidden="true" focusable="false">
      <g
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M11.5 2.25H5.25A1.25 1.25 0 0 0 4 3.5v13a1.25 1.25 0 0 0 1.25 1.25h9.5A1.25 1.25 0 0 0 16 16.5V6.75z" />
        <path d="M11.5 2.25v3.25a1.25 1.25 0 0 0 1.25 1.25H16" />
        <circle cx="9" cy="11" r="2.75" />
        <path d="m11.1 13.1 2 2" />
      </g>
    </svg>
  );
}

export default function App() {
  const { runs, refresh } = useRuns();
  // selection: null | { type: "new" } | { type: "run", id }
  const [selection, setSelection] = useState(null);
  const [config, setConfig] = useState(null);
  const [showKeys, setShowKeys] = useState(false);
  const [showResearch, setShowResearch] = useState(false);
  const [theme, setTheme] = useState(
    () => localStorage.getItem(THEME_KEY) || "light",
  );

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  useEffect(() => {
    api.config().then(setConfig).catch(() => setConfig(null));
  }, []);

  const selectRun = useCallback((id) => setSelection({ type: "run", id }), []);

  const selectedRun =
    selection?.type === "run"
      ? runs.find((r) => r.id === selection.id) || null
      : null;

  let main = (
    <div className="blank">Select a forecast — or start a new one.</div>
  );
  if (selection?.type === "new") {
    main = (
      <NewForecastView
        onCreated={(run, started) => {
          refresh();
          if (run) setSelection({ type: "run", id: run.id });
          if (!run && !started) setSelection(null);
        }}
      />
    );
  } else if (selection?.type === "run" && selectedRun) {
    main =
      selectedRun.status === "backlog" ? (
        <BacklogView
          key={selectedRun.id}
          runId={selectedRun.id}
          onChanged={refresh}
          onDeleted={() => {
            refresh();
            setSelection(null);
          }}
        />
      ) : (
        <RunView
          key={selectedRun.id}
          runId={selectedRun.id}
          onChanged={refresh}
          onDeleted={() => {
            refresh();
            setSelection(null);
          }}
        />
      );
  }

  return (
    <div className="app">
      <header className="hdr">
        <div className="mark">S</div>
        <div className="wordmark">Superforecaster</div>
        <div className="spacer" />
        {config && !config.search_enabled && (
          <span className="chip yellow">no web search</span>
        )}
        <button className="btn tiny" onClick={() => setShowKeys(true)}>
          Keys
        </button>
        <button
          className="btn tiny ghost"
          onClick={() => setTheme(theme === "light" ? "dark" : "light")}
        >
          {theme === "light" ? "Dark" : "Light"}
        </button>
      </header>
      {showKeys && (
        <KeyPanel
          config={config}
          onSaved={setConfig}
          onClose={() => setShowKeys(false)}
        />
      )}
      <div className={`layout ${showResearch && selectedRun ? "docked" : ""}`}>
        <Sidebar
          runs={runs}
          selectedId={selection?.type === "run" ? selection.id : null}
          newSelected={selection?.type === "new"}
          onSelect={selectRun}
          onNew={() => setSelection({ type: "new" })}
        />
        <main className="main">
          <div className="main-inner">{main}</div>
        </main>

        {/* Anchored to the layout rather than the viewport, so "below the nav bar" needs
            no header height to be hardcoded, and so it cannot scroll away with the run. */}
        {selectedRun && (
          <button
            className="btn icon research-toggle"
            aria-expanded={showResearch}
            aria-label={showResearch ? "Hide research" : "View research"}
            title={showResearch ? "Hide research" : "View research"}
            onClick={() => setShowResearch((v) => !v)}
          >
            <ResearchIcon />
          </button>
        )}

        {showResearch && selectedRun && (
          // Keyed on the run: a different run is a different store, so the query resets
          // rather than carrying over.
          <ResearchPanel key={selectedRun.id} runId={selectedRun.id} />
        )}
      </div>
    </div>
  );
}
