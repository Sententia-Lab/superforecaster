import { useCallback, useEffect, useState } from "react";
import { api, getToken, setToken } from "./api.js";
import { useRuns } from "./hooks/useRuns.js";
import Sidebar from "./components/Sidebar.jsx";
import NewForecastView from "./components/NewForecastView.jsx";
import BacklogView from "./components/BacklogView.jsx";
import RunView from "./components/RunView.jsx";

const THEME_KEY = "sf_theme";

export default function App() {
  const { runs, refresh } = useRuns();
  // selection: null | { type: "new" } | { type: "run", id }
  const [selection, setSelection] = useState(null);
  const [config, setConfig] = useState(null);
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

  const onAdminToken = () => {
    const token = window.prompt(
      "Admin token (leave empty to clear):",
      getToken(),
    );
    if (token !== null) setToken(token.trim());
  };

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
        <RunView key={selectedRun.id} runId={selectedRun.id} onChanged={refresh} />
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
        {config?.auth_required && (
          <button className="btn tiny" onClick={onAdminToken}>
            Admin token
          </button>
        )}
        <button
          className="btn tiny ghost"
          onClick={() => setTheme(theme === "light" ? "dark" : "light")}
        >
          {theme === "light" ? "Dark" : "Light"}
        </button>
      </header>
      <div className="layout">
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
      </div>
    </div>
  );
}
