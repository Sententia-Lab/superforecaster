"""Tests for community questions, votes, rate limiting, and IP-based permissions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time

from superforecaster import db


def _future_date(days: int = 30) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def test_submit_and_get_question():
    q = db.submit_question(
        text="Will X happen?",
        resolution_criteria="X is observably true.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    assert q.text == "Will X happen?"
    assert q.status == "pending"
    assert q.net_score == 0
    assert q.user_vote is None or q.user_vote == 0  # ip1 hasn't voted

    fetched = db.get_question(q.id)
    assert fetched is not None
    assert fetched.id == q.id


def test_submit_rate_limit_blocks_second_submission_within_24h():
    db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )

    with pytest.raises(db.RateLimitError):
        db.submit_question(
            text="Q2",
            resolution_criteria="Y.",
            proposed_resolution_date=_future_date(),
            ip_hash="ip1",
        )


def test_submit_rate_limit_resets_after_24h():
    with freeze_time("2026-01-01 00:00:00") as frozen:
        db.submit_question(
            text="Q1",
            resolution_criteria="X.",
            proposed_resolution_date=_future_date(),
            ip_hash="ip1",
        )
        frozen.tick(timedelta(hours=25))
        # Should not raise
        db.submit_question(
            text="Q2",
            resolution_criteria="Y.",
            proposed_resolution_date=_future_date(),
            ip_hash="ip1",
        )


def test_submit_rate_limit_isolated_per_ip():
    db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    # Different IP — allowed
    db.submit_question(
        text="Q2",
        resolution_criteria="Y.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip2",
    )


def test_edit_question_only_by_original_submitter():
    q = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    db.edit_question(q.id, ip_hash="ip1", text="Q1-edited")
    fetched = db.get_question(q.id)
    assert fetched is not None
    assert fetched.text == "Q1-edited"
    assert fetched.edited_at is not None

    with pytest.raises(db.PermissionError):
        db.edit_question(q.id, ip_hash="other-ip", text="hacked")


def test_admin_can_edit_any_question():
    q = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    db.edit_question(q.id, ip_hash=None, text="admin-edit", is_admin=True)
    fetched = db.get_question(q.id)
    assert fetched is not None
    assert fetched.text == "admin-edit"


def test_edit_after_approval_blocked_for_user():
    q = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    db.approve_question(q.id)

    with pytest.raises(db.StateError):
        db.edit_question(q.id, ip_hash="ip1", text="late edit")


def test_delete_soft_deletes_and_excludes_from_lists():
    q = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    db.delete_question(q.id, ip_hash="ip1")

    assert db.get_question(q.id) is None
    listed = db.list_questions()
    assert all(r.id != q.id for r in listed)


def test_delete_only_by_submitter():
    q = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    with pytest.raises(db.PermissionError):
        db.delete_question(q.id, ip_hash="other-ip")


def test_cast_vote_upvote():
    q = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    score = db.cast_vote(q.id, ip_hash="voter1", vote=1)
    assert score == 1


def test_cast_vote_switches_from_upvote_to_downvote():
    q = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    db.cast_vote(q.id, ip_hash="voter1", vote=1)
    new_score = db.cast_vote(q.id, ip_hash="voter1", vote=-1)
    assert new_score == -1
    assert db.get_vote(q.id, "voter1") == -1


def test_remove_vote():
    q = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    db.cast_vote(q.id, ip_hash="voter1", vote=1)
    new_score = db.remove_vote(q.id, ip_hash="voter1")
    assert new_score == 0
    assert db.get_vote(q.id, "voter1") is None


def test_invalid_vote_value_raises():
    q = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    with pytest.raises(ValueError):
        db.cast_vote(q.id, ip_hash="voter1", vote=2)


def test_list_sorted_by_score_descending():
    q1 = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    q2 = db.submit_question(
        text="Q2",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip2",
    )
    q3 = db.submit_question(
        text="Q3",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip3",
    )

    db.cast_vote(q1.id, ip_hash="v1", vote=1)
    db.cast_vote(q1.id, ip_hash="v2", vote=1)
    db.cast_vote(q2.id, ip_hash="v1", vote=-1)
    db.cast_vote(q3.id, ip_hash="v1", vote=1)

    listed = db.list_questions(sort="score")
    assert [r.id for r in listed[:3]] == [q1.id, q3.id, q2.id]
    assert listed[0].net_score == 2
    assert listed[1].net_score == 1
    assert listed[2].net_score == -1


def test_user_vote_populated_when_ip_provided():
    q = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    db.cast_vote(q.id, ip_hash="voter1", vote=1)

    listed = db.list_questions(requester_ip_hash="voter1")
    [our_q] = [r for r in listed if r.id == q.id]
    assert our_q.user_vote == 1
    assert our_q.net_score == 1


def test_top_monthly_returns_at_most_n():
    for i in range(7):
        q = db.submit_question(
            text=f"Q{i}",
            resolution_criteria="X.",
            proposed_resolution_date=_future_date(),
            ip_hash=f"ip{i}",
        )
        for j in range(i + 1):
            db.cast_vote(q.id, ip_hash=f"voter{j}", vote=1)

    top = db.get_top_monthly(n=5)
    assert len(top) == 5
    # Highest-voted first
    assert top[0].text == "Q6"
    assert top[-1].text == "Q2"


def test_approve_question_overrides_resolution_date_and_criteria():
    q = db.submit_question(
        text="Q1",
        resolution_criteria="vague",
        proposed_resolution_date=_future_date(30),
        ip_hash="ip1",
    )
    new_date = _future_date(90)
    approved = db.approve_question(
        q.id, resolution_date=new_date, resolution_criteria="precise criteria"
    )

    assert approved.status == "approved"
    assert approved.resolution_criteria == "precise criteria"
    assert approved.proposed_resolution_date == new_date
    assert approved.approved_at is not None


def test_link_question_to_forecast_requires_approved_status():
    q = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future_date(),
        ip_hash="ip1",
    )
    with pytest.raises(db.StateError):
        db.link_question_to_forecast(q.id, "fake-forecast-id")
