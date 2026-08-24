"""The research store: one row per page a run read (`research_docs`), ranked by an
FTS5 index (`research_index`). `research_id` scopes a store to one run."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import nullcontext
from dataclasses import dataclass

from superforecaster.models import ResearchDoc, ResearchHit

from .db import connect

TITLE_WEIGHT = 5.0
URL_WEIGHT = 2.0
BODY_WEIGHT = 0.5
"""BM25 column weights. A URL ranks below a title because every URL carries `https`
and `com`, which match everything."""

MARK_START = "\x02"
MARK_END = "\x03"
"""STX/ETX wrap a matched run of text. They survive JSON and markdown, and are
stripped from every page on the way in."""


def _scrub(text: str) -> str:
    """A stored page cannot contain the characters that mark a search hit."""
    return text.replace(MARK_START, "").replace(MARK_END, "")


def _match_expression(query: str) -> str:
    """A natural-language query as an FTS5 MATCH expression. Every token is quoted (FTS5
    reads `-` and `AND` as syntax) and joined with OR, so ranking does the filtering."""
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in query)
    tokens = cleaned.split()
    return " OR ".join(f'"{t}"' for t in tokens)


def index_documents(research_id: str, docs: list[ResearchDoc]) -> int:
    """Store documents under one run. Re-storing a URL updates it."""
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
                (research_id, doc.url, _scrub(doc.title), _scrub(doc.body)),
            )
    return sum(1 for d in docs if d.url)


def search_research(
    research_id: str, query: str, limit: int = 5, mark: bool = False
) -> list[ResearchHit]:
    """The documents this run already read, ranked against `query`. `mark` wraps every
    hit in `MARK_START`/`MARK_END` using SQLite's own `highlight()`, for the panel."""
    expression = _match_expression(query)
    if not research_id or not expression:
        return []

    start, end = (MARK_START, MARK_END) if mark else ("", "")
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT highlight(research_index, 0, :start, :end) AS title,
                   highlight(research_index, 1, :start, :end) AS url,
                   highlight(research_index, 2, :start, :end) AS body,
                   d.url AS href,
                   -bm25(research_index, :w_title, :w_url, :w_body) AS score
            FROM research_index
            JOIN research_docs d ON d.rowid = research_index.rowid
            WHERE research_index MATCH :match AND d.research_id = :research_id
            ORDER BY score DESC
            LIMIT :limit
            """,
            {
                "start": start,
                "end": end,
                "w_title": TITLE_WEIGHT,
                "w_url": URL_WEIGHT,
                "w_body": BODY_WEIGHT,
                "match": expression,
                "research_id": research_id,
                "limit": limit,
            },
        ).fetchall()

    return [
        ResearchHit(
            rank=i + 1,
            score=round(r["score"], 4),
            url=r["href"],
            marked_url=r["url"] if mark else None,
            title=r["title"],
            content=r["body"],
        )
        for i, r in enumerate(rows)
    ]


def list_research(
    research_id: str | None, limit: int = 100, offset: int = 0
) -> list[ResearchHit]:
    """Everything the run stored, in insertion order, for the panel."""
    if not research_id:
        return []

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT url, title, body FROM research_docs
            WHERE research_id = ?
            ORDER BY rowid
            LIMIT ? OFFSET ?
            """,
            (research_id, limit, offset),
        ).fetchall()

    return [
        ResearchHit(
            rank=offset + i + 1, url=r["url"], title=r["title"], content=r["body"]
        )
        for i, r in enumerate(rows)
    ]


def count_research(research_id: str | None) -> int:
    """How many pages the run stored. What the panel's header counts."""
    if not research_id:
        return 0
    with connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM research_docs WHERE research_id = ?",
            (research_id,),
        ).fetchone()
    return row["n"]


def delete_research(
    research_id: str | None, conn: sqlite3.Connection | None = None
) -> int:
    """Drop one run's documents, on the caller's connection when given so a forecast
    delete is one transaction."""
    if not research_id:
        return 0
    with nullcontext(conn) if conn is not None else connect() as c:
        cur = c.execute(
            "DELETE FROM research_docs WHERE research_id = ?", (research_id,)
        )
        return cur.rowcount


@dataclass(frozen=True)
class SqliteResearchStore:
    """One run's store, satisfying `superforecaster.deps.ResearchStore`."""

    research_id: str

    def remember(self, docs: list[ResearchDoc]) -> int:
        return index_documents(self.research_id, docs)

    def find(self, query: str, limit: int = 5) -> list[ResearchHit]:
        return search_research(self.research_id, query, limit=limit)


def new_store() -> SqliteResearchStore:
    """A store for a run that is about to start. Its id is what binds it to a forecast."""
    return SqliteResearchStore(str(uuid.uuid4()))


def store_for(research_id: str | None) -> SqliteResearchStore | None:
    """The store a saved forecast was built from, or None if it kept none."""
    return SqliteResearchStore(research_id) if research_id else None
