import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import Accordion from "./Accordion.jsx";
import Prose, { Marked } from "./Prose.jsx";

/**
 * Everything this run has read, docked beside the run rather than over it.
 *
 * The store is the agents' memory of the run — every page `search_web` and
 * `extract_pages` fetched, kept whole. Until this panel the only way to see it was to be
 * an agent calling `search_research`.
 *
 * A dock, not a modal: this is reference material read *against* the forecast, so both
 * have to be legible at once. It takes its own column out of the layout, which narrows
 * the run and re-centres it rather than covering it.
 *
 * The search box runs the *same* BM25 query the agents run, deliberately: the point of
 * looking is to see what the agent would get back, so a different ranking here would
 * answer a different question. It matches title, URL and body, and the server marks every
 * hit — see `Prose`'s `marks`.
 */
export default function ResearchPanel({ runId }) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const searchRef = useRef(null);

  const load = useCallback(
    async (q) => {
      setLoading(true);
      setError("");
      try {
        setPage(await api.runResearch(runId, q));
      } catch (e) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    },
    [runId],
  );

  useEffect(() => {
    searchRef.current?.focus();
  }, []);

  // Debounced, because every keystroke is a BM25 query against the run's whole store.
  useEffect(() => {
    const t = setTimeout(() => load(query.trim()), query ? 250 : 0);
    return () => clearTimeout(t);
  }, [query, load]);

  const results = page?.results || [];
  const total = page?.total || 0;
  const searched = Boolean(page?.query);

  return (
    <aside className="research-dock" aria-label="Research store">
      <div className="dock-title">Research store</div>

      <div className="dock-sub">
        {total === 0
          ? "Nothing stored yet. The agents fill this as they search."
          : searched
            ? `${results.length} of ${total} page${total === 1 ? "" : "s"} — matches in the title, link or text are highlighted`
            : `${total} page${total === 1 ? "" : "s"} this run has read`}
      </div>

      <input
        ref={searchRef}
        className="field-input"
        type="search"
        placeholder="Search titles, links and text…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />

      <div className="dock-body">
        {error && <div className="chip red">{error}</div>}
        {loading && !page && <div className="card-sub">Loading…</div>}
        {!loading && searched && results.length === 0 && (
          <div className="card-sub">
            Nothing here matches. An agent asking this would be told to search the web.
          </div>
        )}
        {results.map((hit) => (
          <Accordion
            // Keyed on the query too, so a new search re-mounts the rows and
            // `defaultOpen` applies again rather than keeping the last state.
            key={`${page?.query || ""}::${hit.url}`}
            className="research-item"
            // Open on a search, closed on a browse: a hit the reader cannot see is a
            // result they have to take on trust, which is the thing this panel exists to
            // remove. Browsing is a list of titles, which is what makes it scannable.
            defaultOpen={searched}
            summary={
              <>
                <span className="grow research-item-title">
                  <Prose inline marks className="prose-inline">
                    {hit.title || hit.url}
                  </Prose>
                </span>
                {hit.score > 0 && (
                  <span className="chip" title="BM25 relevance">
                    {hit.score.toFixed(2)}
                  </span>
                )}
              </>
            }
          >
            <a
              className="research-item-url"
              href={hit.url}
              target="_blank"
              rel="noreferrer noopener"
            >
              <Marked>{hit.marked_url || hit.url}</Marked>
            </a>
            <div className="research-item-body">
              {hit.content ? (
                <Prose marks className="prose tight">
                  {hit.content}
                </Prose>
              ) : (
                <span className="card-sub">(no text was captured for this page)</span>
              )}
            </div>
          </Accordion>
        ))}
      </div>
    </aside>
  );
}
