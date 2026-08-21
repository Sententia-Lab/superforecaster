"""The research store: what a run read, kept and searchable.

A run fetches pages, reads them once, and today throws the text away. This keeps it, so
a later step can find what an earlier one already paid for instead of fetching it again.

Two tables, written by `db.init_db` alongside the other four:

    research_docs    one row per URL per run. The text itself.
    research_index   an FTS5 index over it, kept in step by three triggers.

Two rather than one because FTS5 has no primary key and no usable index on an UNINDEXED
column. A plain table gives `PRIMARY KEY (research_id, url)`, which is both the real
deduplication FTS5 cannot do and the index that keeps a delete from scanning every
forecast's documents.

`research_id` scopes a store to one run. It is not a foreign key to `forecasts(id)`
because the documents are written during the run, and the forecast row does not exist
until synthesis. `db.delete_forecast` calls `delete_research` instead of cascading.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import nullcontext
from dataclasses import dataclass

from superforecaster.models import ResearchDoc, ResearchHit

from .db import connect

TITLE_WEIGHT = 5.0
BODY_WEIGHT = 0.5
"""BM25 column weights. A page whose *title* is about the query answers it more often than
one that mentions it once in the body, and SQLite weights columns rather than letting the
caller re-rank."""


def _match_expression(query: str) -> str:
    """A natural-language query as an FTS5 MATCH expression, or "" if it has no tokens.

    Necessary, not defensive. What the agent writes is prose, and FTS5 reads its
    argument as *syntax*: `US-China tariffs` raises `no such column: China`, and a query
    containing the bare word `AND` is a syntax error. Every token is stripped to
    alphanumerics and quoted, which makes each one a literal.

    Joined with OR, so a document is never dropped for missing a word. Ranking does the
    filtering: `bm25` scores a document matching four tokens above one matching one.
    """
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in query)
    tokens = cleaned.split()
    return " OR ".join(f'"{t}"' for t in tokens)


def index_documents(research_id: str, docs: list[ResearchDoc]) -> int:
    """Store documents under one run. Returns how many rows were written.

    Re-storing a URL updates it rather than adding a second copy — `INSERT OR REPLACE`
    would not, because the FTS5 table it used to live in has no key to conflict on.
    """
    if not research_id or not docs:
        return 0

    with connect() as conn:
        for doc in docs:
            if not doc.url:
                continue
            conn.execute(
                """
                INSERT INTO research_docs (research_id, url, title, body)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (research_id, url) DO UPDATE SET
                    title = excluded.title,
                    body = excluded.body
                """,
                (research_id, doc.url, doc.title, doc.body),
            )
    return sum(1 for d in docs if d.url)


def search_research(research_id: str, query: str, limit: int = 5) -> list[ResearchHit]:
    """The documents this run already read, ranked against `query`.

    Scoped to `research_id`: one run never reads another's store.
    """
    expression = _match_expression(query)
    if not research_id or not expression:
        return []

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT d.url, d.title, d.body,
                   -bm25(research_index, ?, ?) AS score
            FROM research_index
            JOIN research_docs d ON d.rowid = research_index.rowid
            WHERE research_index MATCH ? AND d.research_id = ?
            ORDER BY score DESC
            LIMIT ?
            """,
            (TITLE_WEIGHT, BODY_WEIGHT, expression, research_id, limit),
        ).fetchall()

    return [
        ResearchHit(
            rank=i + 1,
            score=round(r["score"], 4),
            url=r["url"],
            title=r["title"],
            content=r["body"],
        )
        for i, r in enumerate(rows)
    ]


def has_documents(research_id: str | None) -> bool:
    """Whether this run has stored anything yet.

    `withdraw_tools` asks before offering the store to an agent. A store that can only
    answer "nothing yet" costs a tool call to say so.
    """
    if not research_id:
        return False
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM research_docs WHERE research_id = ? LIMIT 1", (research_id,)
        ).fetchone()
    return row is not None


def delete_research(
    research_id: str | None, conn: sqlite3.Connection | None = None
) -> int:
    """Drop one run's documents. Returns how many went. The FTS index follows by trigger.

    Takes an open connection so `db.delete_forecast` can do this and the forecast delete
    in one transaction — a store that outlives its forecast is unreachable, and one that
    dies before it is a half-finished delete.
    """
    if not research_id:
        return 0
    with nullcontext(conn) if conn is not None else connect() as c:
        cur = c.execute(
            "DELETE FROM research_docs WHERE research_id = ?", (research_id,)
        )
        return cur.rowcount


@dataclass(frozen=True)
class SqliteResearchStore:
    """One run's store, satisfying `superforecaster.deps.ResearchStore`.

    Bound to a `research_id` so the tools never pass one around, and so a run cannot name
    another run's store even by accident.
    """

    research_id: str

    def remember(self, docs: list[ResearchDoc]) -> int:
        return index_documents(self.research_id, docs)

    def find(self, query: str, limit: int = 5) -> list[ResearchHit]:
        return search_research(self.research_id, query, limit=limit)

    def is_empty(self) -> bool:
        return not has_documents(self.research_id)


def new_store() -> SqliteResearchStore:
    """A store for a run that is about to start. Its id is what binds it to a forecast."""
    return SqliteResearchStore(str(uuid.uuid4()))


def store_for(research_id: str | None) -> SqliteResearchStore | None:
    """The store a saved forecast was built from, or None if it kept none.

    A refresh, a resolution check, and a postmortem all run months after the run that
    made the forecast. This is how they reach what it read.
    """
    return SqliteResearchStore(research_id) if research_id else None
