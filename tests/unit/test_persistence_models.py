"""Unit tests for the Critic v0 read-side persistence models (Step 2.14, Task 5).

Covers ``DecisionSummary``, ``StoredReview`` and ``StoredCriticReport`` in
``src/persistence/models.py``: field shapes, tz-aware datetime validators,
``extra="forbid"`` / ``frozen=True`` config, and JSON round-trips. Also pins the
``SignalStore`` Protocol surface: both concrete backends must expose every new
method (their bodies land in Tasks 6 and 7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.common.models import SignalDirection, SignalOutcome, SignalStatus
from src.persistence.models import DecisionSummary, StoredCriticReport, StoredReview
from src.persistence.store import AsyncpgSignalStore, DataApiSignalStore, SignalStore

NOW = datetime(2026, 7, 1, tzinfo=UTC)
NAIVE = datetime(2026, 7, 1)


# ---------------------------------------------------------------------------
# DecisionSummary
# ---------------------------------------------------------------------------


def _summary(**overrides: object) -> DecisionSummary:
    base: dict[str, object] = {
        "id": uuid4(),
        "scan_id": uuid4(),
        "symbol": "BTCUSDT",
        "direction": None,
        "status": SignalStatus.SKIPPED,
        "created_at": NOW,
        "violated_rule": None,
        "outcome": None,
        "decision_kind": None,
        "resolution_outcome": None,
        "realized_r": None,
        "entered_at": None,
        "review": None,
        "review_error": None,
    }
    base.update(overrides)
    return DecisionSummary.model_validate(base)


def test_decision_summary_minimal_unresolved_row() -> None:
    row = DecisionSummary(
        id=uuid4(),
        scan_id=uuid4(),
        symbol="BTCUSDT",
        direction=None,
        status=SignalStatus.SKIPPED,
        created_at=NOW,
        violated_rule=None,
        outcome=None,
        decision_kind=None,
        resolution_outcome=None,
        realized_r=None,
        entered_at=None,
        review=None,
        review_error=None,
    )
    assert row.decision_kind is None
    assert row.review is None


def test_decision_summary_full_published_row() -> None:
    row = _summary(
        direction="LONG",
        status="PUBLISHED",
        outcome="WIN",
        decision_kind="PUBLISHED",
        resolution_outcome="WIN",
        realized_r=3.2,
        entered_at=NOW,
        review={"judge_correct": True},
        review_error=None,
    )
    assert row.direction is SignalDirection.LONG
    assert row.status is SignalStatus.PUBLISHED
    assert row.outcome is SignalOutcome.WIN
    assert row.realized_r == pytest.approx(3.2)
    assert row.review == {"judge_correct": True}


@pytest.mark.parametrize("field", ["created_at", "entered_at"])
def test_decision_summary_requires_aware_datetimes(field: str) -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _summary(**{field: NAIVE})


def test_decision_summary_forbids_extra_and_is_frozen() -> None:
    with pytest.raises(ValidationError):
        _summary(unexpected="x")
    row = _summary()
    with pytest.raises(ValidationError):
        row.symbol = "ETHUSDT"  # type: ignore[misc]


def test_decision_summary_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        _summary(status="OPEN")


def test_decision_summary_json_round_trip() -> None:
    row = _summary(direction="SHORT", status="PUBLISHED", realized_r=-1.0, entered_at=NOW)
    assert DecisionSummary.model_validate(row.model_dump(mode="json")) == row


# ---------------------------------------------------------------------------
# StoredReview
# ---------------------------------------------------------------------------


def _review(**overrides: object) -> StoredReview:
    base: dict[str, object] = {
        "signal_id": uuid4(),
        "decision_kind": "PUBLISHED",
        "review": {"lesson": "x"},
        "model": "claude-sonnet-4-5",
        "token_usage": {"input": 10, "output": 5},
        "cost_usd": 0.0012,
        "error": None,
        "attempts": 1,
        "created_at": NOW,
    }
    base.update(overrides)
    return StoredReview.model_validate(base)


def test_stored_review_requires_aware_datetime() -> None:
    with pytest.raises(ValidationError):
        StoredReview(
            signal_id=uuid4(),
            decision_kind="PUBLISHED",
            review={},
            model="m",
            token_usage={},
            cost_usd=None,
            error=None,
            attempts=1,
            created_at=datetime(2026, 7, 1),
        )


def test_stored_review_errored_row_has_empty_review() -> None:
    row = _review(review={}, error="boom", cost_usd=None, attempts=2)
    assert row.error == "boom"
    assert row.attempts == 2
    assert row.review == {}


def test_stored_review_attempts_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError):
        _review(attempts=0)


def test_stored_review_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        _review(unexpected=1)


# ---------------------------------------------------------------------------
# StoredCriticReport
# ---------------------------------------------------------------------------


def test_stored_critic_report_round_trip() -> None:
    rep = StoredCriticReport(
        id=uuid4(),
        period_start=NOW,
        period_end=NOW,
        report={"summary": "x"},
        markdown="# r",
        pr_url=None,
        created_at=NOW,
    )
    assert StoredCriticReport.model_validate(rep.model_dump(mode="json")) == rep


@pytest.mark.parametrize("field", ["period_start", "period_end", "created_at"])
def test_stored_critic_report_requires_aware_datetimes(field: str) -> None:
    kwargs: dict[str, object] = {
        "id": uuid4(),
        "period_start": NOW,
        "period_end": NOW,
        "report": {},
        "markdown": "",
        "pr_url": "https://github.com/o/r/pull/1",
        "created_at": NOW,
    }
    kwargs[field] = NAIVE
    with pytest.raises(ValidationError, match="timezone-aware"):
        StoredCriticReport.model_validate(kwargs)


# ---------------------------------------------------------------------------
# SignalStore protocol surface -- both backends expose every new method
# ---------------------------------------------------------------------------

CRITIC_STORE_METHODS = (
    "list_unresolved_decisions",
    "count_unresolved_decisions",
    "list_agent_runs_for_scan",
    "save_resolution",
    "get_resolution",
    "close_active_setup_for_signal",
    "list_pending_reviews",
    "count_pending_reviews",
    "save_review",
    "list_decision_summaries",
    "list_scan_runs_between",
    "list_active_setups",
    "save_critic_report",
)


@pytest.mark.parametrize("name", CRITIC_STORE_METHODS)
def test_protocol_declares_critic_methods(name: str) -> None:
    assert callable(getattr(SignalStore, name))


@pytest.mark.parametrize("backend", [AsyncpgSignalStore, DataApiSignalStore])
@pytest.mark.parametrize("name", CRITIC_STORE_METHODS)
def test_both_backends_expose_critic_methods(backend: type, name: str) -> None:
    assert callable(getattr(backend, name))
