"""Tests for the research store: what a run read, kept and searchable.

The store exists so a later stage can read a page an earlier one already fetched. Three
things have to hold for that to be worth anything: the search finds documents by meaning
rather than by exact wording, one run never sees another's pages, and deleting a forecast
takes its pages with it.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import db, research
from superforecaster.models import ResearchDoc

RID = "run-1"
OTHER = "run-2"


def _doc(url: str, title: str = "", body: str = "") -> ResearchDoc:
    return ResearchDoc(url=url, title=title, body=body)


@pytest.fixture
def stocked():
    """A store holding four pages, three of them about steel tariffs."""
    research.index_documents(
        RID,
        [
            _doc(
                "https://ustr.gov/exclusions",
                title="Steel tariff exclusions",
                body="The US exclusion process for steel tariffs was renewed in 2026.",
            ),
            _doc(
                "https://reuters.com/talks",
                title="China trade talks",
                body="Beijing and Washington resumed tariff negotiations on steel.",
            ),
            _doc(
                "https://example.com/prices",
                title="Steel prices",
                body="Steel prices rose after the tariffs took effect.",
            ),
            _doc(
                "https://example.com/shoes",
                title="Shoe review",
                body="Running shoes for marathon training.",
            ),
        ],
    )
    research.index_documents(
        OTHER,
        [_doc("https://ustr.gov/exclusions", title="Another run", body="steel tariff")],
    )


# ---------- the schema ----------


def test_init_is_idempotent():
    """`init_db` runs on every connection, so it must be safe to run twice."""
    db.init_db()
    db.init_db()

    research.index_documents(RID, [_doc("https://a", title="a", body="alpha")])
    assert len(research.search_research(RID, "alpha")) == 1


def test_migration_from_v4_keeps_its_forecasts(tmp_path, monkeypatch):
    """An existing database gains the tables without losing what it held.

    The create block only builds tables a fresh database is missing, so a database that
    already exists reaches the new schema through `MIGRATIONS` or not at all.
    """
    old = tmp_path / "v4.db"
    conn = sqlite3.connect(old)
    conn.executescript("""
        CREATE TABLE forecasts (
            id TEXT PRIMARY KEY, question TEXT NOT NULL,
            resolution_criteria TEXT NOT NULL, resolution_source TEXT NOT NULL,
            category TEXT NOT NULL, submission_gap_days INTEGER NOT NULL DEFAULT 7,
            submission_deadline TIMESTAMP NOT NULL, resolution_date TIMESTAMP NOT NULL,
            resolved_at TIMESTAMP, outcome REAL,
            is_ambiguous INTEGER NOT NULL DEFAULT 0, scored_probability REAL,
            brier_score REAL, last_refreshed_at TIMESTAMP,
            flagged_for_resolution_review INTEGER NOT NULL DEFAULT 0,
            initial_reasoning TEXT NOT NULL, decompositions_json TEXT NOT NULL,
            research_json TEXT NOT NULL, created_at TIMESTAMP NOT NULL
        );
        INSERT INTO forecasts VALUES (
            'f1','Q?','crit','src','test',7,'2026-01-01T00:00:00+00:00',
            '2026-06-01T00:00:00+00:00',NULL,NULL,0,NULL,NULL,NULL,0,
            'why','[]','{}','2025-12-01T00:00:00+00:00'
        );
        PRAGMA user_version = 4;
    """)
    conn.commit()
    conn.close()

    monkeypatch.setenv("DATABASE_PATH", str(old))
    db.init_db()

    with db.connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert c.execute("SELECT count(*) FROM forecasts").fetchone()[0] == 1
        assert c.execute("SELECT research_id FROM forecasts").fetchone()[0] is None

    research.index_documents(RID, [_doc("https://a", title="a", body="alpha")])
    assert len(research.search_research(RID, "alpha")) == 1


def test_migration_5_to_6_keeps_the_pages_and_indexes_the_url(tmp_path, monkeypatch):
    """Adding a column to an FTS5 table means rebuilding it, and the rebuild must not cost
    a run its research — those pages cannot be fetched again."""
    old = tmp_path / "v5.db"
    monkeypatch.setenv("DATABASE_PATH", str(old))
    db.init_db()
    research.index_documents(
        RID, [_doc("https://www.reuters.com/steel", title="Trade", body="Geneva talks")]
    )

    # Put it back the way version 5 built it: no `url` column in the index.
    with db.connect() as c:
        c.executescript("""
            DROP TRIGGER research_ai; DROP TRIGGER research_ad; DROP TRIGGER research_au;
            DROP TABLE research_index;
            CREATE VIRTUAL TABLE research_index USING fts5(
                title, body, content='research_docs', content_rowid='rowid',
                tokenize='porter unicode61');
            CREATE TRIGGER research_ai AFTER INSERT ON research_docs BEGIN
                INSERT INTO research_index(rowid,title,body) VALUES(new.rowid,new.title,new.body);
            END;
        """)
        c.execute("INSERT INTO research_index(research_index) VALUES('rebuild')")
        c.execute("PRAGMA user_version = 5")

    assert research.search_research(RID, "reuters") == [], "v5 could not search a URL"

    db.init_db()

    with db.connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert c.execute("SELECT count(*) FROM research_docs").fetchone()[0] == 1
    assert [h.url for h in research.search_research(RID, "reuters")] == [
        "https://www.reuters.com/steel"
    ]
    assert research.search_research(
        RID, "Geneva"
    ), "the rebuild kept the body searchable"


# ---------- searching ----------


def test_search_ranks_by_relevance(stocked):
    """A page whose title is about the query beats one that mentions it in passing."""
    hits = research.search_research(RID, "steel tariff exclusions")

    assert hits[0].url == "https://ustr.gov/exclusions"
    assert hits[0].score > hits[-1].score
    assert all(h.rank == i + 1 for i, h in enumerate(hits))


def test_partial_token_match_still_returns(stocked):
    """No page here contains every word of this query, and it must still answer.

    An all-tokens rule would return nothing for the kind of long, specific query an
    agent actually writes. Ranking filters, not matching.
    """
    hits = research.search_research(
        RID, "US-China steel tariff negotiations 2026 outlook"
    )

    assert len(hits) == 3
    assert "https://example.com/shoes" not in {h.url for h in hits}


@pytest.mark.parametrize(
    "query",
    ["US-China tariffs", "Trump: what next?", "AND", "steel OR", '"', "   ", ""],
)
def test_natural_language_never_raises(stocked, query):
    """FTS5 reads its argument as syntax, and the agent writes prose.

    Unescaped, `US-China tariffs` raises `no such column: China` and a bare `AND` is a
    syntax error. Both are ordinary things for a model to type.
    """
    assert isinstance(research.search_research(RID, query), list)


def test_no_query_touches_nothing(stocked):
    assert research.search_research(RID, "!!!") == []
    assert research.search_research("", "steel") == []


# ---------- scope ----------


def test_scoped_to_research_id(stocked):
    """Two runs, two stores. Neither can name the other's."""
    mine = {h.url for h in research.search_research(RID, "steel")}
    theirs = research.search_research(OTHER, "steel")

    assert len(mine) == 3
    assert [h.title for h in theirs] == ["Another run"]


def test_same_url_two_forecasts(stocked):
    """`UNIQUE` is per run, so two runs may each hold the same page."""
    with db.connect() as c:
        rows = c.execute(
            "SELECT research_id FROM research_docs WHERE url = ?",
            ("https://ustr.gov/exclusions",),
        ).fetchall()

    assert {r["research_id"] for r in rows} == {RID, OTHER}


def test_reindex_same_url_updates_it(stocked):
    """Storing a page twice leaves one row. FTS5 alone would leave two."""
    research.index_documents(
        RID, [_doc("https://ustr.gov/exclusions", title="Revised", body="new body")]
    )

    with db.connect() as c:
        rows = c.execute(
            "SELECT title FROM research_docs WHERE research_id = ? AND url = ?",
            (RID, "https://ustr.gov/exclusions"),
        ).fetchall()

    assert [r["title"] for r in rows] == ["Revised"]
    assert [h.url for h in research.search_research(RID, "new body")] == [
        "https://ustr.gov/exclusions"
    ]


def test_has_documents(stocked):
    assert research.has_documents(RID)
    assert not research.has_documents("never-used")
    assert not research.has_documents(None)


# ---------- deleting ----------


def test_delete_research_leaves_other_runs(stocked):
    assert research.delete_research(RID) == 4

    assert research.search_research(RID, "steel") == []
    assert len(research.search_research(OTHER, "steel")) == 1


def test_delete_leaves_no_orphan_index_rows(stocked):
    """The FTS index is external content, kept in step by triggers.

    Without the delete trigger the rows would go and the index would keep answering for
    them, which surfaces as a search returning pages that no longer exist.
    """
    research.delete_research(RID)

    with db.connect() as c:
        n = c.execute(
            "SELECT count(*) FROM research_index WHERE research_index MATCH 'steel'"
        ).fetchone()[0]

    assert n == 1


# ---------- why a result is here ----------


def test_search_matches_the_url_not_only_the_text(stocked):
    """A domain is often the whole reason a person recognises a source."""
    assert [h.url for h in research.search_research(RID, "reuters")] == [
        "https://reuters.com/talks"
    ]


def test_every_matched_field_is_marked(stocked):
    """Title, URL and body each carry their own hits, so a reader can see which one put
    the row on screen."""
    hit = research.search_research(RID, "reuters talks", mark=True)[0]

    assert research.MARK_START in hit.marked_url
    assert "reuters" in _marked(hit.marked_url)
    assert "China trade talks" == hit.title.replace(research.MARK_START, "").replace(
        research.MARK_END, ""
    )
    assert "talks" in _marked(hit.title)


def test_marking_follows_the_stemmer_not_the_letters():
    """The index is stemmed, so "negotiations" matches a page that says "negotiating".

    Only the tokenizer knows which characters that was. A literal highlighter would
    return the row and mark nothing in it, which reads as a wrong result.
    """
    store = research.new_store()
    store.remember(
        [ResearchDoc(url="https://a", title="Tariff negotiating session", body="x")]
        + [ResearchDoc(url=f"https://b{i}", title="other", body="y") for i in range(8)]
    )

    hit = research.search_research(store.research_id, "negotiations", mark=True)[0]

    assert _marked(hit.title) == ["negotiating"]


def test_a_browse_marks_nothing(stocked):
    """Nothing was searched, so nothing can be shown as the reason."""
    for hit in research.list_research(RID):
        assert research.MARK_START not in hit.title
        assert hit.marked_url is None


def test_a_page_cannot_smuggle_the_marks_in(stocked):
    """The markers are ours. A page carrying them would fake a hit."""
    research.index_documents(
        RID,
        [
            _doc(
                "https://evil",
                title=f"Real{research.MARK_START}fake{research.MARK_END}",
                body="alpha",
            )
        ],
    )

    hit = research.search_research(RID, "alpha", mark=True)[0]
    assert research.MARK_START not in hit.title
    assert hit.title == "Realfakе".replace("е", "e")


def _marked(text: str) -> list[str]:
    """The runs of text a search marked, in order."""
    import re

    return re.findall(
        f"{research.MARK_START}(.*?){research.MARK_END}", text or "", re.S
    )
