# Decision Review System (Critic v0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Score every trading decision the system has made against real price action (deterministic), grade the reasoning with Claude, and deliver a Critic report + proposed-rules PR — all running in AWS Lambda and GitHub Actions, never on a laptop.

**Architecture:** Layer 1 (`OutcomeResolver`) runs as a new `{"mode":"resolve"}` event mode of the existing scan Lambda in ap-south-1: it pulls unresolved decisions from Aurora, fetches 5m Binance candles, replays entry/stop/target and writes `decision_resolutions` (+ `signals.outcome` for published ones). Layer 2 (`DecisionReviewer` + `CriticReport`) runs in a GitHub Actions workflow: it reads Aurora via the Data API, makes one Claude call per decision into `decision_reviews`, aggregates into a markdown report, opens a PR on `claude/proposed-rules-<date>` and posts to Telegram. Both layers are idempotent and chunked; the queue is the data itself.

**Tech Stack:** Python 3.11, Pydantic v2, Anthropic SDK via `src/common/llm.structured_completion`, asyncpg + RDS Data API dual-backend `SignalStore`, ccxt (Binance USDT-M), AWS CDK (Python) + EventBridge Scheduler, GitHub Actions + `gh` CLI, pytest + ruff + mypy --strict.

**Spec:** `docs/superpowers/specs/2026-09-22-decision-review-critic-v0-design.md`

## Global Constraints

- Signal-only system. Nothing here places, sizes or simulates *orders* for execution; it scores decisions already made (SPEC §6.8).
- Traditional indicators (RSI, MACD, Bollinger, MAs) are forbidden anywhere in `src/` (SPEC §1.6).
- All agents use `DEFAULT_MODEL` from `src/common/llm.py` (`claude-sonnet-4-5`); never call `AsyncAnthropic.messages.create` directly — always `structured_completion`.
- Every LLM call's cost must land in `agent_runs` (`agent_role='critic'`) — NFR-2.3.
- Pydantic v2 at every boundary; models `extra="forbid"`, `frozen=True` unless stated.
- Both persistence backends (`AsyncpgSignalStore`, `DataApiSignalStore`) implement every new `SignalStore` method; RDS Data API rejects array params and needs `::text`/`::uuid`/`::timestamptz`/`::jsonb` casts on every typed param (see existing methods in `src/persistence/store.py`).
- Schema changes are idempotent (`CREATE TABLE IF NOT EXISTS`, `ALTER ... ADD COLUMN IF NOT EXISTS`) and must be listed in `EXPECTED_TABLES` (`src/persistence/__init__.py`) — `tests/unit/test_migrate.py` asserts it.
- Universal checkpoints before every commit: `ruff check .`, `ruff format --check .`, `mypy --strict src/ scripts/`, `pytest -q` all green. `tests/` is **not** in mypy scope. Run them from the repo root with the venv: `.\.venv\Scripts\python.exe -m <tool>` on Windows, `python -m <tool>` on Linux.
- Commit format: `type(scope): summary` + body, ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Branch: `feat/slice-2-step-2.14-critic-v0` (already created).
- Never use `git add -A` / `git add .` — two zero-byte junk files (`'violated_rule'`, `**Purpose.**`) sit untracked in the repo root; add files explicitly.
- Region is `ap-south-1`; GitHub runners cannot reach Binance (HTTP 451) — candle fetching lives only in the Lambda.
- No new runtime dependencies. If one is truly needed it must be added to BOTH `pyproject.toml` and `.pre-commit-config.yaml` mypy `additional_dependencies`.

## File map

| Path | Responsibility | Task |
|---|---|---|
| `src/agents/critic/__init__.py` | package exports | 1 |
| `src/agents/critic/models.py` | `DecisionKind`, `ResolutionOutcome`, `Resolution`, `DecisionRecord`, `FailureCategory`, `SkepticVerdict`, `DecisionReview`, `ProposedRuleChange`, `CriticReport`, `GateCriterion`, `ExitGateReport` | 1 |
| `src/agents/critic/replay.py` | pure price-path engine `replay()` | 2 |
| `src/agents/critic/inventory.py` | `classify()` a signal + its agent runs into a `DecisionRecord` | 3 |
| `src/providers/base.py`, `src/providers/binance.py` | `fetch_klines(symbol, timeframe, start, end)` | 4 |
| `src/persistence/schema.sql`, `models.py`, `__init__.py`, `store.py` (Protocol) | 3 tables, `DecisionSummary`, `StoredReview`, protocol methods | 5 |
| `src/persistence/repositories.py`, `store.py` (`AsyncpgSignalStore`) | asyncpg implementations | 6 |
| `src/persistence/store.py` (`DataApiSignalStore`) | Data API implementations | 7 |
| `src/agents/critic/resolver.py` | `OutcomeResolver.run(chunk_size)` | 8 |
| `scripts/run_scan.py` | `{"mode":"resolve"}` dispatch | 9 |
| `src/agents/critic/exit_gate.py` | Slice 2 gate criteria (pure) | 10 |
| `src/agents/critic/prompts.py`, `reviewer.py` | per-decision Claude review | 11 |
| `src/agents/critic/report.py` | aggregates, `CriticReport` call, markdown | 12 |
| `scripts/run_critic.py` | CLI `review` / `report` / `exit-gate` | 13 |
| `infrastructure/stacks/scheduling_stack.py` | `rate(2 days)` resolve schedule | 14 |
| `.github/workflows/critic.yml`, docs, SPEC | orchestration + documentation | 15 |

## Shared reference (read before any task)

**How agent outputs are stored.** `scripts/run_scan.py::_persist` writes one `signals` row per (scan, symbol) and one `agent_runs` row per agent that ran, all sharing `scan_id`. Each scan run covers exactly one symbol, so `scan_id` identifies one decision. Payload keys:

- `SignalProposal` payload (`signals.direction IS NOT NULL`): `proposal_id, scan_id, strategy, created_at, symbol, direction, entry_price, stop_loss, take_profit_1, take_profit_2, risk_reward_ratio, leverage, risk_percent, tags, confluence_narrative, features`.
- `SkipDecision` payload (`signals.direction IS NULL`): `decision_id, scan_id, strategy, symbol, reason, details, violated_rule, created_at`.
- Judge `agent_runs.output`: `ruling ∈ {PUBLISH, PUBLISH_WITH_CAVEAT, SKIP}`, `confidence`, `reasoning`, `caveat`.
- Skeptic output: `severity, recommends_against, headline, reasoning, cited_macro` (or a `NoMacroData` dict with `provider`, `reason`).
- Historian output: `sample_size, wins, losses, breakeven, inconclusive, win_rate, matches, summary`.
- Analyzer output on a gate rejection: the **rejected `SignalProposal`** (has `direction`).
- `signals.status` is `PUBLISHED` for *any* proposal payload, including Judge vetoes. Do not use it to decide publication.

**Existing helpers to reuse:** `src/common/llm.structured_completion` (returns `StructuredCompletionResult(output, model, tokens_in, tokens_out, cost_usd, attempts, latency_ms)`); `_str_param/_long_param/_double_param/_utc_iso/_parse_records/_parse_jsonb_field/_to_jsonb` in `src/persistence/store.py`; `FakeRdsDataClient`/`make_store`/`params_by_name`/`response(...)` in `tests/unit/test_data_api_store.py`; `_store()`/`_conn()` in `tests/unit/test_asyncpg_store.py`; `FakeStore`, `_client(responses)`, `_response(payload)` in `tests/unit/test_forecaster.py`.

---

### Task 1: Critic models

**Files:**
- Create: `src/agents/critic/__init__.py`
- Create: `src/agents/critic/models.py`
- Test: `tests/unit/test_critic_models.py`

**Interfaces:**
- Produces (used by every later task):
  - `DecisionKind(StrEnum)`: `PUBLISHED, VETOED, GATE_REJECTED, NO_SETUP`
  - `ResolutionOutcome(StrEnum)`: `WIN, LOSS, EXPIRED, UNRESOLVABLE`
  - `Resolution(BaseModel)`: `signal_id: UUID, decision_kind: DecisionKind, outcome: ResolutionOutcome, decided_at: datetime, entered_at: datetime | None, resolved_at: datetime, realized_r: float, max_adverse_excursion_r: float | None, max_favorable_excursion_r: float | None, tp2_reached: bool, candles_used: int (ge=0), engine_version: str`
  - `DecisionRecord(BaseModel, frozen=False, arbitrary types allowed)`: `signal: StoredSignal, kind: DecisionKind, proposal: SignalProposal | None, skip: SkipDecision | None, historian: dict | None, skeptic: dict | None, judge: dict | None`
  - `SkepticVerdict(StrEnum)`: `YES, NO, NOT_APPLICABLE`
  - `FailureCategory(StrEnum)`: `NONE, BAD_STRUCTURE_READ, WRONG_ZONE, RR_UNREALISTIC, IGNORED_SKEPTIC, OVERRULED_GOOD_SETUP, MACRO_MISSED, EXPIRED_NO_TRIGGER, OTHER`
  - `DecisionReview(BaseModel)`: `smc_read_valid: bool, skeptic_vindicated: SkepticVerdict, judge_correct: bool, gate_correct: bool | None = None, failure_category: FailureCategory, lesson: str (min 5, max 300), confidence: JudgeConfidence`
  - `ProposedRuleChange(BaseModel)`: `target: str (min 1, max 120), change: str (min 10, max 2000), rationale: str (min 10, max 2000), evidence_signal_ids: list[UUID]`
  - `CriticReport(BaseModel)`: `summary: str (min 20, max 4000), key_patterns: list[str] (max 20 items), skeptic_assessment: str, judge_assessment: str, gate_assessment: str, proposed_changes: list[ProposedRuleChange] (max 10)`
  - `GateCriterion(BaseModel)`: `number: int (1..4), title: str, passed: bool, evidence: str`
  - `ExitGateReport(BaseModel)`: `criteria: list[GateCriterion] (len 4), all_passed: bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_critic_models.py
"""Schema tests for the Critic v0 models (SPEC design 2026-09-22 §3, §5.3, §6.2)."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.agents.critic.models import (
    CriticReport,
    DecisionKind,
    DecisionReview,
    ExitGateReport,
    FailureCategory,
    GateCriterion,
    ProposedRuleChange,
    Resolution,
    ResolutionOutcome,
    SkepticVerdict,
)
from src.common.models import JudgeConfidence


def _resolution(**overrides: object) -> Resolution:
    base: dict[str, object] = {
        "signal_id": uuid4(),
        "decision_kind": DecisionKind.PUBLISHED,
        "outcome": ResolutionOutcome.WIN,
        "decided_at": datetime(2026, 7, 1, 8, 3, tzinfo=UTC),
        "entered_at": datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        "resolved_at": datetime(2026, 7, 1, 15, 0, tzinfo=UTC),
        "realized_r": 3.0,
        "max_adverse_excursion_r": -0.4,
        "max_favorable_excursion_r": 3.2,
        "tp2_reached": False,
        "candles_used": 84,
        "engine_version": "replay-v1",
    }
    base.update(overrides)
    return Resolution(**base)  # type: ignore[arg-type]


def test_resolution_round_trips_json() -> None:
    res = _resolution()
    dumped = res.model_dump(mode="json")
    assert dumped["outcome"] == "WIN"
    assert Resolution.model_validate(dumped) == res


def test_resolution_rejects_naive_datetimes() -> None:
    with pytest.raises(ValidationError):
        _resolution(decided_at=datetime(2026, 7, 1, 8, 3))


def test_resolution_rejects_negative_candles() -> None:
    with pytest.raises(ValidationError):
        _resolution(candles_used=-1)


def test_resolution_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _resolution(bogus=1)


def test_decision_kind_values() -> None:
    assert {k.value for k in DecisionKind} == {"PUBLISHED", "VETOED", "GATE_REJECTED", "NO_SETUP"}


def test_resolution_outcome_values() -> None:
    assert {o.value for o in ResolutionOutcome} == {"WIN", "LOSS", "EXPIRED", "UNRESOLVABLE"}


def test_decision_review_minimal() -> None:
    review = DecisionReview(
        smc_read_valid=True,
        skeptic_vindicated=SkepticVerdict.NOT_APPLICABLE,
        judge_correct=True,
        failure_category=FailureCategory.NONE,
        lesson="Clean discount-zone long; nothing to change.",
        confidence=JudgeConfidence.HIGH,
    )
    assert review.gate_correct is None


def test_decision_review_lesson_length_bounds() -> None:
    with pytest.raises(ValidationError):
        DecisionReview(
            smc_read_valid=True,
            skeptic_vindicated=SkepticVerdict.NO,
            judge_correct=True,
            failure_category=FailureCategory.NONE,
            lesson="x" * 301,
            confidence=JudgeConfidence.LOW,
        )


def test_critic_report_caps_proposed_changes() -> None:
    change = ProposedRuleChange(
        target="JUDGE_SYSTEM_PROMPT",
        change="Require the Judge to cite the Skeptic's headline when overruling it.",
        rationale="Overruled HIGH-severity objections lost 4 of 5 times in the period.",
        evidence_signal_ids=[uuid4()],
    )
    with pytest.raises(ValidationError):
        CriticReport(
            summary="s" * 40,
            key_patterns=["p"],
            skeptic_assessment="ok",
            judge_assessment="ok",
            gate_assessment="ok",
            proposed_changes=[change] * 11,
        )


def test_exit_gate_report_requires_exactly_four_criteria() -> None:
    crit = GateCriterion(number=1, title="7 days autonomous", passed=True, evidence="16/day")
    with pytest.raises(ValidationError):
        ExitGateReport(criteria=[crit], all_passed=True)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_critic_models.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.agents.critic'`

- [ ] **Step 3: Write the models**

```python
# src/agents/critic/__init__.py
"""Critic v0: deterministic outcome resolution + Claude decision review.

Design: docs/superpowers/specs/2026-09-22-decision-review-critic-v0-design.md
"""

from src.agents.critic.models import (
    CriticReport,
    DecisionKind,
    DecisionRecord,
    DecisionReview,
    ExitGateReport,
    FailureCategory,
    GateCriterion,
    ProposedRuleChange,
    Resolution,
    ResolutionOutcome,
    SkepticVerdict,
)

__all__ = [
    "CriticReport",
    "DecisionKind",
    "DecisionRecord",
    "DecisionReview",
    "ExitGateReport",
    "FailureCategory",
    "GateCriterion",
    "ProposedRuleChange",
    "Resolution",
    "ResolutionOutcome",
    "SkepticVerdict",
]
```

```python
# src/agents/critic/models.py
"""Pydantic models for Critic v0 (design §3, §5.3, §6.2, §6.4)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.common.models import JudgeConfidence, SignalProposal
from src.common.models.skip_decision import SkipDecision
from src.persistence.models import StoredSignal


class DecisionKind(StrEnum):
    """How a (scan, symbol) decision ended, derived from the Judge ruling."""

    PUBLISHED = "PUBLISHED"  # Judge PUBLISH / PUBLISH_WITH_CAVEAT -> Telegram
    VETOED = "VETOED"  # proposal existed, Judge ruled SKIP
    GATE_REJECTED = "GATE_REJECTED"  # proposal existed, a SPEC §1.6 hard rule blocked it
    NO_SETUP = "NO_SETUP"  # Analyzer found nothing (SkipDecision without violated_rule)


class ResolutionOutcome(StrEnum):
    """Deterministic price-replay result (design §5.3)."""

    WIN = "WIN"
    LOSS = "LOSS"
    EXPIRED = "EXPIRED"
    UNRESOLVABLE = "UNRESOLVABLE"  # proposal missing/corrupt; never retried


def _aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (use UTC)")
    return v


class Resolution(BaseModel):
    """One row of decision_resolutions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_id: UUID
    decision_kind: DecisionKind
    outcome: ResolutionOutcome
    decided_at: datetime
    entered_at: datetime | None = None
    resolved_at: datetime
    realized_r: float
    max_adverse_excursion_r: float | None = None
    max_favorable_excursion_r: float | None = None
    tp2_reached: bool = False
    candles_used: int = Field(ge=0)
    engine_version: str = Field(min_length=1, max_length=40)

    @field_validator("decided_at", "resolved_at")
    @classmethod
    def _aware_required(cls, v: datetime) -> datetime:
        return _aware(v)

    @field_validator("entered_at")
    @classmethod
    def _aware_optional(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _aware(v)


class DecisionRecord(BaseModel):
    """A signal row stitched to the agent outputs that produced it (design §3)."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    signal: StoredSignal
    kind: DecisionKind
    proposal: SignalProposal | None = None
    skip: SkipDecision | None = None
    historian: dict[str, Any] | None = None
    skeptic: dict[str, Any] | None = None
    judge: dict[str, Any] | None = None


class SkepticVerdict(StrEnum):
    YES = "YES"
    NO = "NO"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class FailureCategory(StrEnum):
    NONE = "NONE"
    BAD_STRUCTURE_READ = "BAD_STRUCTURE_READ"
    WRONG_ZONE = "WRONG_ZONE"
    RR_UNREALISTIC = "RR_UNREALISTIC"
    IGNORED_SKEPTIC = "IGNORED_SKEPTIC"
    OVERRULED_GOOD_SETUP = "OVERRULED_GOOD_SETUP"
    MACRO_MISSED = "MACRO_MISSED"
    EXPIRED_NO_TRIGGER = "EXPIRED_NO_TRIGGER"
    OTHER = "OTHER"


class DecisionReview(BaseModel):
    """Claude's graded judgement of one decision, given its resolution (design §6.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    smc_read_valid: bool = Field(description="Was the Analyzer's SMC structure read defensible?")
    skeptic_vindicated: SkepticVerdict = Field(
        description="Did the market bear out the Skeptic's objection? NOT_APPLICABLE if no Skeptic ran."
    )
    judge_correct: bool = Field(description="Given the outcome, was the Judge's ruling right?")
    gate_correct: bool | None = Field(
        default=None, description="GATE_REJECTED only: did the rule save us from a loss?"
    )
    failure_category: FailureCategory
    lesson: str = Field(min_length=5, max_length=300)
    confidence: JudgeConfidence


class ProposedRuleChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    target: str = Field(
        min_length=1,
        max_length=120,
        description="SKEPTIC_SYSTEM_PROMPT | JUDGE_SYSTEM_PROMPT | FORECASTER_SYSTEM_PROMPT | "
        "risk_gates.<CONSTANT> | SPEC §1.6 rule N",
    )
    change: str = Field(min_length=10, max_length=2000)
    rationale: str = Field(min_length=10, max_length=2000)
    evidence_signal_ids: list[UUID] = Field(default_factory=list, max_length=50)


class CriticReport(BaseModel):
    """Aggregate weekly/period judgement (design §6.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    summary: str = Field(min_length=20, max_length=4000)
    key_patterns: list[str] = Field(default_factory=list, max_length=20)
    skeptic_assessment: str = Field(min_length=1, max_length=2000)
    judge_assessment: str = Field(min_length=1, max_length=2000)
    gate_assessment: str = Field(min_length=1, max_length=2000)
    proposed_changes: list[ProposedRuleChange] = Field(default_factory=list, max_length=10)


class GateCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int = Field(ge=1, le=4)
    title: str = Field(min_length=1, max_length=120)
    passed: bool
    evidence: str = Field(min_length=1, max_length=2000)


class ExitGateReport(BaseModel):
    """SPEC §5 'End of Slice 2' verdict (design §6.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criteria: list[GateCriterion] = Field(min_length=4, max_length=4)
    all_passed: bool

    @model_validator(mode="after")
    def _all_passed_matches(self) -> ExitGateReport:
        if self.all_passed != all(c.passed for c in self.criteria):
            raise ValueError("all_passed must equal all(criteria.passed)")
        return self
```

- [ ] **Step 4: Run the tests and the checkpoints**

Run: `python -m pytest tests/unit/test_critic_models.py -q && python -m ruff check src/agents/critic tests/unit/test_critic_models.py && python -m ruff format --check src/agents/critic tests/unit/test_critic_models.py && python -m mypy --strict src/`
Expected: all PASS, zero warnings.

- [ ] **Step 5: Commit**

```bash
git add src/agents/critic/__init__.py src/agents/critic/models.py tests/unit/test_critic_models.py
git commit -m "feat(critic): add Critic v0 models (Resolution, DecisionReview, CriticReport)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Replay engine (pure)

**Files:**
- Create: `src/agents/critic/replay.py`
- Test: `tests/unit/test_critic_replay.py`

**Interfaces:**
- Consumes: `SignalProposal` (`src/common/models`), `Kline` (`src/providers.base`), `ResolutionOutcome` (Task 1).
- Produces:
  - constants `ENGINE_VERSION = "replay-v1"`, `ENTRY_VALIDITY = timedelta(hours=72)`, `MAX_HOLD = timedelta(days=7)`, `CANDLE_STEP = timedelta(minutes=5)`
  - `@dataclass(frozen=True) ReplayResult(outcome: ResolutionOutcome | None, entered_at, resolved_at, realized_r: float, max_adverse_excursion_r: float | None, max_favorable_excursion_r: float | None, tp2_reached: bool, candles_used: int)` — `outcome is None` means *pending* (window still open, nothing hit yet).
  - `replay(*, proposal, decided_at, candles, now, entry_validity=ENTRY_VALIDITY, max_hold=MAX_HOLD) -> ReplayResult`

Rules (design §5.3): candles are filtered to `decided_at <= open_time < decided_at + max_hold` and sorted. Entry touched when `low <= entry <= high`; gap-through (first eligible candle opens beyond entry in the trade's favour: long `open < entry`, short `open > entry`) fills at that candle's `open`. Not entered by `decided_at + entry_validity` → EXPIRED with `realized_r = 0`. After entry, per candle: stop touched (`long: low <= stop`, `short: high >= stop`) checked **before** target (`long: high >= tp1`, `short: low <= tp1`); both in one candle → LOSS. R is measured from the fill price with the *planned* risk `|entry - stop|` as the unit. MAE/MFE accumulate from the entry candle onward (clamped to ≤0 / ≥0). `tp2_reached` is set only on candles that did not also hit the stop. If the window runs out: if `now >= decided_at + max_hold` → EXPIRED (realized_r = mark-to-market R at the last close if entered, else 0), otherwise pending (`outcome=None`). Empty candle list: same rule (EXPIRED if window closed, else pending).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_critic_replay.py
"""Exhaustive tests for the deterministic price-path replay (design §5.3)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.agents.critic.models import ResolutionOutcome
from src.agents.critic.replay import (
    ENGINE_VERSION,
    ENTRY_VALIDITY,
    MAX_HOLD,
    ReplayResult,
    replay,
)
from src.common.models import SignalDirection, SignalProposal
from src.providers.base import Kline

T0 = datetime(2026, 7, 1, 8, 3, tzinfo=UTC)
STEP = timedelta(minutes=5)


def _proposal(direction: SignalDirection = SignalDirection.LONG) -> SignalProposal:
    if direction is SignalDirection.LONG:
        entry, stop, tp1, tp2 = 100.0, 98.0, 106.0, 110.0
    else:
        entry, stop, tp1, tp2 = 100.0, 102.0, 94.0, 90.0
    return SignalProposal(
        scan_id=uuid4(),
        strategy="smc",
        symbol="BTCUSDT",
        direction=direction,
        entry_price=entry,
        stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2,
        risk_reward_ratio=3.0,
        leverage=3.0,
        risk_percent=1.0,
        tags=["fvg"],
        confluence_narrative="test",
        features={},
    )


def _c(i: int, o: float, h: float, lo: float, c: float) -> Kline:
    return Kline(open_time=T0 + i * STEP, open=o, high=h, low=lo, close=c, volume=1.0)


def _flat(n: int, price: float = 101.0, start: int = 0) -> list[Kline]:
    return [_c(start + i, price, price + 0.1, price - 0.1, price) for i in range(n)]


NOW_LATE = T0 + MAX_HOLD + timedelta(hours=1)
NOW_EARLY = T0 + timedelta(hours=1)


def test_long_target_first_is_win_with_r_from_geometry() -> None:
    candles = [_c(0, 101, 101.5, 100.0, 100.5), _c(1, 100.5, 106.2, 100.2, 105.0)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.WIN
    assert res.entered_at == T0
    assert res.resolved_at == T0 + STEP
    assert res.realized_r == pytest.approx(3.0)
    assert res.tp2_reached is False
    assert res.candles_used == 2


def test_long_stop_first_is_loss() -> None:
    candles = [_c(0, 101, 101.5, 100.0, 100.5), _c(1, 100.5, 100.8, 97.9, 98.5)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.LOSS
    assert res.realized_r == pytest.approx(-1.0)


def test_both_in_one_candle_is_loss_and_tp2_not_counted() -> None:
    candles = [_c(0, 100.0, 111.0, 97.0, 105.0)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.LOSS
    assert res.tp2_reached is False


def test_short_symmetry_target_first() -> None:
    candles = [_c(0, 99.5, 100.2, 99.0, 99.2), _c(1, 99.2, 99.5, 93.8, 94.5)]
    res = replay(
        proposal=_proposal(SignalDirection.SHORT), decided_at=T0, candles=candles, now=NOW_EARLY
    )
    assert res.outcome is ResolutionOutcome.WIN
    assert res.realized_r == pytest.approx(3.0)


def test_short_stop_first_is_loss() -> None:
    candles = [_c(0, 99.5, 100.2, 99.0, 99.2), _c(1, 99.2, 102.1, 99.0, 101.0)]
    res = replay(
        proposal=_proposal(SignalDirection.SHORT), decided_at=T0, candles=candles, now=NOW_EARLY
    )
    assert res.outcome is ResolutionOutcome.LOSS


def test_entry_never_touched_expires_after_validity() -> None:
    n = int(ENTRY_VALIDITY / STEP) + 2
    res = replay(proposal=_proposal(), decided_at=T0, candles=_flat(n, 101.0), now=NOW_LATE)
    assert res.outcome is ResolutionOutcome.EXPIRED
    assert res.entered_at is None
    assert res.realized_r == 0.0
    assert res.resolved_at == T0 + ENTRY_VALIDITY


def test_entry_never_touched_and_window_open_is_pending() -> None:
    res = replay(proposal=_proposal(), decided_at=T0, candles=_flat(3, 101.0), now=NOW_EARLY)
    assert res.outcome is None


def test_entry_never_touched_no_candles_after_validity_but_now_past_it_expires() -> None:
    res = replay(
        proposal=_proposal(),
        decided_at=T0,
        candles=_flat(3, 101.0),
        now=T0 + ENTRY_VALIDITY + timedelta(minutes=1),
    )
    assert res.outcome is ResolutionOutcome.EXPIRED


def test_gap_through_entry_fills_at_open() -> None:
    # Long: first candle opens below entry (favourable gap) and never trades back to 100.
    candles = [_c(0, 99.0, 99.5, 98.5, 99.2), _c(1, 99.2, 106.5, 99.0, 106.0)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.WIN
    assert res.entered_at == T0
    # fill 99.0, target 106, unit risk 2 -> (106-99)/2 = 3.5
    assert res.realized_r == pytest.approx(3.5)


def test_tp2_reached_flag() -> None:
    candles = [_c(0, 100.0, 100.5, 99.9, 100.2), _c(1, 100.2, 110.5, 100.1, 109.0)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.WIN
    assert res.tp2_reached is True


def test_mae_mfe_tracked_after_entry() -> None:
    candles = [
        _c(0, 100.0, 100.5, 99.9, 100.2),  # entry
        _c(1, 100.2, 102.0, 99.0, 101.0),  # MAE -0.5R, MFE +1R
        _c(2, 101.0, 106.1, 100.5, 105.0),  # win
    ]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.max_adverse_excursion_r == pytest.approx(-0.5)
    assert res.max_favorable_excursion_r == pytest.approx(3.05)


def test_entered_but_nothing_hit_expires_at_max_hold_with_mark_to_market() -> None:
    n = int(MAX_HOLD / STEP)
    candles = [_c(0, 100.0, 100.5, 99.9, 100.2), *_flat(n, 101.0, start=1)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_LATE)
    assert res.outcome is ResolutionOutcome.EXPIRED
    assert res.entered_at == T0
    assert res.realized_r == pytest.approx(0.5)  # (101-100)/2


def test_entered_and_window_open_is_pending() -> None:
    candles = [_c(0, 100.0, 100.5, 99.9, 100.2), *_flat(5, 101.0, start=1)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is None
    assert res.entered_at == T0


def test_candles_before_decided_at_are_ignored() -> None:
    early = Kline(open_time=T0 - STEP, open=100, high=107, low=97, close=100, volume=1)
    res = replay(proposal=_proposal(), decided_at=T0, candles=[early], now=NOW_EARLY)
    assert res.outcome is None
    assert res.candles_used == 0


def test_unsorted_candles_are_sorted() -> None:
    c1 = _c(1, 100.5, 106.2, 100.2, 105.0)
    c0 = _c(0, 101, 101.5, 100.0, 100.5)
    res = replay(proposal=_proposal(), decided_at=T0, candles=[c1, c0], now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.WIN
    assert res.entered_at == T0


def test_empty_candles_window_closed_expires() -> None:
    res = replay(proposal=_proposal(), decided_at=T0, candles=[], now=NOW_LATE)
    assert res.outcome is ResolutionOutcome.EXPIRED
    assert res.candles_used == 0


def test_engine_version_constant() -> None:
    assert ENGINE_VERSION == "replay-v1"
    assert isinstance(ReplayResult.__dataclass_fields__, dict)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_critic_replay.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.agents.critic.replay'`

- [ ] **Step 3: Write the engine**

```python
# src/agents/critic/replay.py
"""Deterministic price-path replay for one proposal (design §5.3).

Pure function over candles -- no I/O, no LLM. Whether price touched the
target before the stop is arithmetic; keeping it here (and versioned via
ENGINE_VERSION) means outcomes can be re-derived when the rules change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.agents.critic.models import ResolutionOutcome
from src.common.models import SignalDirection, SignalProposal
from src.providers.base import Kline

ENGINE_VERSION = "replay-v1"
ENTRY_VALIDITY = timedelta(hours=72)
MAX_HOLD = timedelta(days=7)
CANDLE_STEP = timedelta(minutes=5)


@dataclass(frozen=True)
class ReplayResult:
    """Outcome of replaying one proposal. ``outcome is None`` == still pending."""

    outcome: ResolutionOutcome | None
    entered_at: datetime | None
    resolved_at: datetime | None
    realized_r: float
    max_adverse_excursion_r: float | None
    max_favorable_excursion_r: float | None
    tp2_reached: bool
    candles_used: int


def replay(
    *,
    proposal: SignalProposal,
    decided_at: datetime,
    candles: Sequence[Kline],
    now: datetime,
    entry_validity: timedelta = ENTRY_VALIDITY,
    max_hold: timedelta = MAX_HOLD,
) -> ReplayResult:
    is_long = proposal.direction is SignalDirection.LONG
    entry = proposal.entry_price
    stop = proposal.stop_loss
    tp1 = proposal.take_profit_1
    tp2 = proposal.take_profit_2
    unit_risk = abs(entry - stop)
    if unit_risk <= 0:  # pragma: no cover - SignalProposal geometry validator forbids this
        raise ValueError("proposal has zero risk distance")

    hold_end = decided_at + max_hold
    entry_deadline = decided_at + entry_validity
    window = sorted(
        (c for c in candles if decided_at <= c.open_time < hold_end), key=lambda c: c.open_time
    )

    fill: float | None = None
    entered_at: datetime | None = None
    mae: float | None = None
    mfe: float | None = None
    tp2_hit = False
    used = 0

    def r_of(price: float) -> float:
        assert fill is not None
        return (price - fill) / unit_risk if is_long else (fill - price) / unit_risk

    for c in window:
        if fill is None:
            if c.open_time >= entry_deadline:
                return ReplayResult(
                    ResolutionOutcome.EXPIRED, None, entry_deadline, 0.0, None, None, False, used
                )
            used += 1
            touched = c.low <= entry <= c.high
            gap_through = (c.open < entry) if is_long else (c.open > entry)
            if not (touched or gap_through):
                continue
            fill = entry if touched else c.open
            entered_at = c.open_time
        else:
            used += 1

        adverse = r_of(c.low if is_long else c.high)
        favorable = r_of(c.high if is_long else c.low)
        mae = min(adverse, 0.0) if mae is None else min(mae, adverse)
        mfe = max(favorable, 0.0) if mfe is None else max(mfe, favorable)

        stop_hit = (c.low <= stop) if is_long else (c.high >= stop)
        tp1_hit = (c.high >= tp1) if is_long else (c.low <= tp1)
        if stop_hit:
            return ReplayResult(
                ResolutionOutcome.LOSS, entered_at, c.open_time, r_of(stop), mae, mfe, tp2_hit, used
            )
        if tp2 is not None and ((c.high >= tp2) if is_long else (c.low <= tp2)):
            tp2_hit = True
        if tp1_hit:
            return ReplayResult(
                ResolutionOutcome.WIN, entered_at, c.open_time, r_of(tp1), mae, mfe, tp2_hit, used
            )

    # Ran out of candles.
    if fill is None:
        if now >= entry_deadline:
            return ReplayResult(
                ResolutionOutcome.EXPIRED, None, entry_deadline, 0.0, None, None, False, used
            )
        return ReplayResult(None, None, None, 0.0, None, None, False, used)

    if now >= hold_end:
        last_close = window[-1].close
        return ReplayResult(
            ResolutionOutcome.EXPIRED, entered_at, hold_end, r_of(last_close), mae, mfe, tp2_hit, used
        )
    return ReplayResult(None, entered_at, None, 0.0, mae, mfe, tp2_hit, used)
```

- [ ] **Step 4: Run the tests and checkpoints**

Run: `python -m pytest tests/unit/test_critic_replay.py -q && python -m ruff check src/agents/critic tests/unit/test_critic_replay.py && python -m ruff format --check src/agents/critic tests/unit/test_critic_replay.py && python -m mypy --strict src/`
Expected: all PASS. If `test_mae_mfe_tracked_after_entry` fails on the MFE value, the expected value is `(106.1 - 100) / 2 = 3.05` — fix the test only if the engine's arithmetic is demonstrably right.

- [ ] **Step 5: Commit**

```bash
git add src/agents/critic/replay.py tests/unit/test_critic_replay.py
git commit -m "feat(critic): deterministic price-path replay engine (replay-v1)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Decision inventory (classification)

**Files:**
- Create: `src/agents/critic/inventory.py`
- Test: `tests/unit/test_critic_inventory.py`

**Interfaces:**
- Consumes: `StoredSignal`, `StoredAgentRun` (`src/persistence.models`), `AgentRole`, `JudgeRuling` (`src/common.models`), `DecisionKind`, `DecisionRecord` (Task 1).
- Produces: `classify(signal: StoredSignal, runs: Sequence[StoredAgentRun]) -> DecisionRecord`; `is_scoreable(kind: DecisionKind) -> bool` (True for PUBLISHED / VETOED / GATE_REJECTED).

Rules: payload with `"direction"` key → proposal. Judge run present and `ruling == "SKIP"` → VETOED; any other ruling, or no Judge run (Slice-1-era rows) → PUBLISHED. Payload without `"direction"` → skip; `violated_rule` non-null → GATE_REJECTED with `proposal` taken from the Analyzer run output if that output has a `"direction"` key (else `None`); otherwise NO_SETUP. Historian/Skeptic/Judge outputs are attached as raw dicts when present. Corrupt proposal payloads (`ValidationError`) fall back to `proposal=None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_critic_inventory.py
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from src.agents.critic.inventory import classify, is_scoreable
from src.agents.critic.models import DecisionKind
from src.common.models import AgentRole, SignalDirection, SignalStatus
from src.persistence.models import StoredAgentRun, StoredSignal

NOW = datetime(2026, 7, 1, 8, 3, tzinfo=UTC)


def _proposal_payload(scan_id: Any) -> dict[str, Any]:
    return {
        "proposal_id": str(uuid4()),
        "scan_id": str(scan_id),
        "strategy": "smc",
        "created_at": NOW.isoformat(),
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit_1": 106.0,
        "take_profit_2": None,
        "risk_reward_ratio": 3.0,
        "leverage": 3.0,
        "risk_percent": 1.0,
        "tags": ["fvg"],
        "confluence_narrative": "test",
        "features": {},
    }


def _skip_payload(scan_id: Any, violated_rule: str | None) -> dict[str, Any]:
    return {
        "decision_id": str(uuid4()),
        "scan_id": str(scan_id),
        "strategy": "smc",
        "symbol": "BTCUSDT",
        "reason": "INSUFFICIENT_RR" if violated_rule else "NO_CLEAR_BIAS",
        "details": "gate rejected the proposal" if violated_rule else "no valid structure",
        "violated_rule": violated_rule,
        "created_at": NOW.isoformat(),
    }


def _signal(payload: dict[str, Any], scan_id: Any) -> StoredSignal:
    return StoredSignal(
        id=uuid4(),
        scan_id=scan_id,
        symbol="BTCUSDT",
        strategy="smc",
        direction=SignalDirection.LONG if "direction" in payload else None,
        status=SignalStatus.PUBLISHED if "direction" in payload else SignalStatus.SKIPPED,
        created_at=NOW,
        payload=payload,
        tags=[],
        features={},
    )


def _run(scan_id: Any, role: AgentRole, output: dict[str, Any]) -> StoredAgentRun:
    return StoredAgentRun(
        id=uuid4(),
        scan_id=scan_id,
        agent_role=role,
        strategy="smc",
        input_hash="h",
        output=output,
        latency_ms=0,
        token_usage={},
        cost_usd=None,
        created_at=NOW,
    )


def test_published_when_judge_publishes() -> None:
    sid = uuid4()
    rec = classify(
        _signal(_proposal_payload(sid), sid),
        [_run(sid, AgentRole.JUDGE, {"ruling": "PUBLISH_WITH_CAVEAT", "confidence": "MEDIUM", "reasoning": "ok", "caveat": "c"})],
    )
    assert rec.kind is DecisionKind.PUBLISHED
    assert rec.proposal is not None and rec.proposal.entry_price == 100.0
    assert rec.judge is not None and rec.judge["ruling"] == "PUBLISH_WITH_CAVEAT"


def test_vetoed_when_judge_skips_even_though_status_is_published() -> None:
    sid = uuid4()
    sig = _signal(_proposal_payload(sid), sid)
    assert sig.status is SignalStatus.PUBLISHED  # the known status bug
    rec = classify(sig, [_run(sid, AgentRole.JUDGE, {"ruling": "SKIP", "confidence": "HIGH", "reasoning": "no", "caveat": None})])
    assert rec.kind is DecisionKind.VETOED


def test_published_when_no_judge_run_slice1_era() -> None:
    sid = uuid4()
    rec = classify(_signal(_proposal_payload(sid), sid), [])
    assert rec.kind is DecisionKind.PUBLISHED


def test_gate_rejected_pulls_proposal_from_analyzer_run() -> None:
    sid = uuid4()
    rec = classify(
        _signal(_skip_payload(sid, "RULE_2_MIN_RR"), sid),
        [_run(sid, AgentRole.ANALYZER, _proposal_payload(sid))],
    )
    assert rec.kind is DecisionKind.GATE_REJECTED
    assert rec.proposal is not None
    assert rec.skip is not None and rec.skip.violated_rule == "RULE_2_MIN_RR"


def test_gate_rejected_without_analyzer_proposal_has_none() -> None:
    sid = uuid4()
    rec = classify(_signal(_skip_payload(sid, "RULE_7_SESSION_BLOCK"), sid), [])
    assert rec.kind is DecisionKind.GATE_REJECTED
    assert rec.proposal is None


def test_no_setup() -> None:
    sid = uuid4()
    rec = classify(_signal(_skip_payload(sid, None), sid), [])
    assert rec.kind is DecisionKind.NO_SETUP
    assert rec.proposal is None and rec.skip is not None


def test_attaches_historian_and_skeptic() -> None:
    sid = uuid4()
    rec = classify(
        _signal(_proposal_payload(sid), sid),
        [
            _run(sid, AgentRole.HISTORIAN, {"sample_size": 3, "win_rate": 0.66}),
            _run(sid, AgentRole.SKEPTIC, {"severity": "HIGH", "headline": "DXY breakout"}),
            _run(sid, AgentRole.JUDGE, {"ruling": "PUBLISH"}),
        ],
    )
    assert rec.historian == {"sample_size": 3, "win_rate": 0.66}
    assert rec.skeptic is not None and rec.skeptic["severity"] == "HIGH"


def test_corrupt_proposal_payload_yields_none_proposal() -> None:
    sid = uuid4()
    bad = _proposal_payload(sid)
    bad["stop_loss"] = "not-a-number"
    rec = classify(_signal(bad, sid), [])
    assert rec.kind is DecisionKind.PUBLISHED
    assert rec.proposal is None


def test_is_scoreable() -> None:
    assert is_scoreable(DecisionKind.PUBLISHED)
    assert is_scoreable(DecisionKind.VETOED)
    assert is_scoreable(DecisionKind.GATE_REJECTED)
    assert not is_scoreable(DecisionKind.NO_SETUP)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_critic_inventory.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the classifier**

```python
# src/agents/critic/inventory.py
"""Stitch a signals row to its agent_runs and classify the decision (design §3).

``signals.status`` is PUBLISHED for *every* SignalProposal payload, including
Judge vetoes (see repositories.create_signal). The Judge's own agent_runs row is
therefore the only trustworthy source for "was it published".
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from src.agents.critic.models import DecisionKind, DecisionRecord
from src.common.models import AgentRole, JudgeRuling, SignalProposal
from src.common.models.skip_decision import SkipDecision
from src.persistence.models import StoredAgentRun, StoredSignal

logger = logging.getLogger(__name__)

_SCOREABLE = frozenset({DecisionKind.PUBLISHED, DecisionKind.VETOED, DecisionKind.GATE_REJECTED})


def is_scoreable(kind: DecisionKind) -> bool:
    """True when the decision carries a proposal that can be price-replayed."""
    return kind in _SCOREABLE


def _first_output(runs: Sequence[StoredAgentRun], role: AgentRole) -> dict[str, Any] | None:
    for run in runs:
        if run.agent_role is role:
            return dict(run.output)
    return None


def _proposal_from(payload: dict[str, Any] | None, *, signal_id: Any) -> SignalProposal | None:
    if payload is None or "direction" not in payload:
        return None
    try:
        return SignalProposal.model_validate(payload)
    except ValidationError as exc:
        logger.warning("signal %s: proposal payload failed validation: %s", signal_id, exc)
        return None


def classify(signal: StoredSignal, runs: Sequence[StoredAgentRun]) -> DecisionRecord:
    historian = _first_output(runs, AgentRole.HISTORIAN)
    skeptic = _first_output(runs, AgentRole.SKEPTIC)
    judge = _first_output(runs, AgentRole.JUDGE)
    payload = dict(signal.payload)

    if "direction" in payload:
        ruling = judge.get("ruling") if judge is not None else None
        kind = DecisionKind.VETOED if ruling == JudgeRuling.SKIP.value else DecisionKind.PUBLISHED
        return DecisionRecord(
            signal=signal,
            kind=kind,
            proposal=_proposal_from(payload, signal_id=signal.id),
            skip=None,
            historian=historian,
            skeptic=skeptic,
            judge=judge,
        )

    try:
        skip: SkipDecision | None = SkipDecision.model_validate(payload)
    except ValidationError as exc:
        logger.warning("signal %s: skip payload failed validation: %s", signal.id, exc)
        skip = None

    violated = payload.get("violated_rule")
    if violated:
        analyzer_output = _first_output(runs, AgentRole.ANALYZER)
        return DecisionRecord(
            signal=signal,
            kind=DecisionKind.GATE_REJECTED,
            proposal=_proposal_from(analyzer_output, signal_id=signal.id),
            skip=skip,
            historian=historian,
            skeptic=skeptic,
            judge=judge,
        )
    return DecisionRecord(
        signal=signal,
        kind=DecisionKind.NO_SETUP,
        proposal=None,
        skip=skip,
        historian=historian,
        skeptic=skeptic,
        judge=judge,
    )
```

- [ ] **Step 4: Run the tests and checkpoints**

Run: `python -m pytest tests/unit/test_critic_inventory.py -q && python -m ruff check . && python -m ruff format --check . && python -m mypy --strict src/`
Expected: PASS. (`INSUFFICIENT_RR` and `NO_CLEAR_BIAS` are real `SkipReason` members in `src/common/models/skip_decision.py`.)

- [ ] **Step 5: Commit**

```bash
git add src/agents/critic/inventory.py tests/unit/test_critic_inventory.py
git commit -m "feat(critic): classify decisions from signals + agent_runs (Judge-veto aware)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```


---

### Task 4: Historical candle fetch on the provider

**Files:**
- Modify: `src/providers/base.py` (add abstract `fetch_klines` to `DataProvider`, after `fetch_market_snapshot`)
- Modify: `src/providers/binance.py` (implement paging)
- Test: `tests/unit/test_binance_provider.py` (append), `tests/unit/test_provider_base.py` (append)

**Interfaces:**
- Produces: `DataProvider.fetch_klines(self, symbol: str, timeframe: Timeframe, *, start: datetime, end: datetime) -> list[Kline]` — candles with `start <= open_time < end`, ascending, no duplicates. Raises `ValueError` if `end <= start`. Provider errors surface as the existing `ProviderError` hierarchy.

Binance implementation: page with `self._client.fetch_ohlcv(symbol, timeframe.value, since=<ms>, limit=1000)`; each page costs `self._klines_weight(1000)` from `self._bucket`; advance `since` to `last_open_ms + step_ms`; stop when a page is empty, shorter than requested, or the last candle's open + step ≥ `end`. Drop candles outside `[start, end)`. Translate `ccxt.BaseError` via `self._translate_ccxt_error`. Reuse `self._normalize_ohlcv`. Step ms per timeframe: `{"1d": 86_400_000, "4h": 14_400_000, "1h": 3_600_000, "15m": 900_000, "5m": 300_000}` as a module constant `_TIMEFRAME_MS`.

Any other **concrete** `DataProvider` subclass in `src/providers/` (grep `(DataProvider)`) must get a `fetch_klines` that raises `ProviderError("fetch_klines not supported", provider=self.name)` so it still instantiates; the macro providers (FRED etc.) are the likely candidates. Test fakes under `tests/` that subclass `DataProvider` need the same stub.

- [ ] **Step 1: Write the failing tests**

Read the first 80 lines of `tests/unit/test_binance_provider.py` to learn how it builds a `BinanceProvider` around a fake exchange, then add this helper next to the file's existing factory (adapt the constructor kwarg to what the file uses):

```python
def _provider_with_ohlcv(*, side_effect: Any) -> tuple[BinanceProvider, MagicMock]:
    exchange = MagicMock()
    exchange.fetch_ohlcv = AsyncMock(side_effect=side_effect)
    exchange.close = AsyncMock()
    provider = BinanceProvider(client=exchange)
    return provider, exchange
```

Append:

```python
class TestFetchKlines:
    def _rows(self, start_ms: int, n: int, step_ms: int = 300_000) -> list[list[float]]:
        return [[start_ms + i * step_ms, 100.0, 101.0, 99.0, 100.5, 1.0] for i in range(n)]

    async def test_pages_until_end_and_filters_window(self) -> None:
        start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        end = start + timedelta(minutes=5 * 1500)
        start_ms = int(start.timestamp() * 1000)
        page1 = self._rows(start_ms, 1000)
        page2 = self._rows(start_ms + 1000 * 300_000, 1000)  # overshoots end
        provider, exchange = _provider_with_ohlcv(side_effect=[page1, page2])
        klines = await provider.fetch_klines("BTCUSDT", Timeframe.M5, start=start, end=end)
        assert len(klines) == 1500
        assert klines[0].open_time == start
        assert klines[-1].open_time < end
        assert exchange.fetch_ohlcv.await_count == 2
        second_call = exchange.fetch_ohlcv.await_args_list[1]
        assert second_call.kwargs["since"] == start_ms + 1000 * 300_000
        assert second_call.kwargs["limit"] == 1000

    async def test_stops_on_short_page(self) -> None:
        start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        end = start + timedelta(days=30)
        rows = self._rows(int(start.timestamp() * 1000), 10)
        provider, exchange = _provider_with_ohlcv(side_effect=[rows])
        klines = await provider.fetch_klines("BTCUSDT", Timeframe.M5, start=start, end=end)
        assert len(klines) == 10
        assert exchange.fetch_ohlcv.await_count == 1

    async def test_rejects_inverted_window(self) -> None:
        provider, _ = _provider_with_ohlcv(side_effect=[[]])
        t = datetime(2026, 7, 1, tzinfo=UTC)
        with pytest.raises(ValueError):
            await provider.fetch_klines("BTCUSDT", Timeframe.M5, start=t, end=t)

    async def test_translates_ccxt_errors(self) -> None:
        provider, _ = _provider_with_ohlcv(side_effect=ccxt.NetworkError("down"))
        t = datetime(2026, 7, 1, tzinfo=UTC)
        with pytest.raises(ProviderError):
            await provider.fetch_klines(
                "BTCUSDT", Timeframe.M5, start=t, end=t + timedelta(hours=1)
            )
```

Append to `tests/unit/test_provider_base.py` one test asserting that a `DataProvider` subclass implementing only `fetch_market_snapshot` and `fetch_macro_context` cannot be instantiated (`TypeError` from the ABC) — mirror how that file already tests abstractness.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_binance_provider.py -q -k FetchKlines`
Expected: FAIL — `AttributeError: 'BinanceProvider' object has no attribute 'fetch_klines'`

- [ ] **Step 3: Implement**

`src/providers/base.py`, inside `DataProvider` after `fetch_market_snapshot`:

```python
    @abstractmethod
    async def fetch_klines(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
    ) -> list[Kline]:
        """Historical candles with ``start <= open_time < end``, ascending.

        Used by the Critic's OutcomeResolver to replay a decision against what
        the market did afterwards. Raises ValueError when ``end <= start``.
        """
```

`src/providers/binance.py` (module constants near the existing weight table, method inside `BinanceProvider`):

```python
_TIMEFRAME_MS: Final[dict[Timeframe, int]] = {
    Timeframe.D1: 86_400_000,
    Timeframe.H4: 14_400_000,
    Timeframe.H1: 3_600_000,
    Timeframe.M15: 900_000,
    Timeframe.M5: 300_000,
}
_PAGE_LIMIT: Final[int] = 1000


    async def fetch_klines(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
    ) -> list[Kline]:
        if end <= start:
            raise ValueError(f"end must be after start (got {start} .. {end})")
        step_ms = _TIMEFRAME_MS[timeframe]
        since = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        out: list[Kline] = []
        while since < end_ms:
            await self._bucket.acquire(self._klines_weight(_PAGE_LIMIT))
            try:
                raw = await self._client.fetch_ohlcv(
                    symbol, timeframe.value, since=since, limit=_PAGE_LIMIT
                )
            except ccxt.BaseError as exc:
                raise self._translate_ccxt_error(exc) from exc
            page = self._normalize_ohlcv(raw, timeframe=timeframe)
            if not page:
                break
            out.extend(k for k in page if start <= k.open_time < end)
            last_ms = int(page[-1].open_time.timestamp() * 1000)
            if len(page) < _PAGE_LIMIT or last_ms + step_ms >= end_ms:
                break
            since = last_ms + step_ms
        return out
```

Add `fetch_klines` stubs raising `ProviderError` to every other concrete `DataProvider` in `src/providers/` and to test fakes that subclass it.

- [ ] **Step 4: Run tests and checkpoints**

Run: `python -m pytest -q && python -m ruff check . && python -m ruff format --check . && python -m mypy --strict src/ scripts/`
Expected: the **whole** suite green (adding an abstract method can break unrelated fakes).

- [ ] **Step 5: Commit**

```bash
git add src/providers/base.py src/providers/binance.py tests/unit/test_binance_provider.py tests/unit/test_provider_base.py
# plus any other provider / fake files you touched
git commit -m "feat(providers): fetch_klines historical candle paging for outcome replay

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Schema, persistence models and SignalStore protocol

**Files:**
- Modify: `src/persistence/schema.sql` (append three tables + indexes)
- Modify: `src/persistence/__init__.py` (`EXPECTED_TABLES` + exports)
- Modify: `src/persistence/models.py` (add `DecisionSummary`, `StoredReview`, `StoredCriticReport`)
- Modify: `src/persistence/store.py` (`SignalStore` Protocol: new methods; temporary `NotImplementedError` bodies in both concrete stores)
- Test: `tests/unit/test_migrate.py` (update expected table set), `tests/unit/test_persistence_models.py` (create)

**Interfaces (Produces — Tasks 6 and 7 implement exactly these):**

```python
# src/persistence/models.py  (all: model_config = ConfigDict(extra="forbid", frozen=True); tz-aware validators like StoredActiveSetup)
class DecisionSummary(BaseModel):
    """Light row for aggregates: signals LEFT JOIN decision_resolutions LEFT JOIN decision_reviews."""
    id: UUID
    scan_id: UUID
    symbol: str
    direction: SignalDirection | None
    status: SignalStatus
    created_at: datetime
    violated_rule: str | None
    outcome: SignalOutcome | None            # signals.outcome
    decision_kind: str | None                # decision_resolutions.decision_kind
    resolution_outcome: str | None           # decision_resolutions.outcome
    realized_r: float | None
    entered_at: datetime | None
    review: dict[str, Any] | None            # decision_reviews.review; None when unreviewed or errored
    review_error: str | None

class StoredReview(BaseModel):
    signal_id: UUID
    decision_kind: str
    review: dict[str, Any]
    model: str
    token_usage: dict[str, Any]
    cost_usd: float | None
    error: str | None
    attempts: int
    created_at: datetime

class StoredCriticReport(BaseModel):
    id: UUID
    period_start: datetime
    period_end: datetime
    report: dict[str, Any]
    markdown: str
    pr_url: str | None
    created_at: datetime
```

```python
# SignalStore Protocol additions (src/persistence/store.py), after update_active_setup
async def list_unresolved_decisions(self, *, limit: int, decided_before: datetime) -> list[StoredSignal]: ...
async def count_unresolved_decisions(self, *, decided_before: datetime) -> int: ...
async def list_agent_runs_for_scan(self, scan_id: UUID) -> list[StoredAgentRun]: ...
async def save_resolution(self, resolution: Resolution) -> None: ...   # upsert on signal_id
async def get_resolution(self, signal_id: UUID) -> Resolution | None: ...
async def close_active_setup_for_signal(
    self, *, signal_id: UUID, status: ActiveSetupStatus, evaluation: Mapping[str, Any]
) -> None: ...   # UPDATE ... WHERE signal_id = $1 AND status = 'OPEN'
async def list_pending_reviews(
    self, *, limit: int, no_setup_before: datetime, retry_before: datetime
) -> list[StoredSignal]: ...
async def count_pending_reviews(self, *, no_setup_before: datetime, retry_before: datetime) -> int: ...
async def save_review(
    self, *, signal_id: UUID, decision_kind: str, review: Mapping[str, Any], model: str,
    token_usage: Mapping[str, Any], cost_usd: float | None, error: str | None,
) -> None: ...   # upsert; ON CONFLICT attempts = decision_reviews.attempts + 1, created_at = NOW()
async def list_decision_summaries(
    self, *, since: datetime, until: datetime, after: tuple[datetime, UUID] | None, limit: int
) -> list[DecisionSummary]: ...
async def list_scan_runs_between(
    self, *, since: datetime, until: datetime, after: tuple[datetime, UUID] | None, limit: int
) -> list[StoredScanRun]: ...
async def list_active_setups(self, *, after: tuple[datetime, UUID] | None, limit: int) -> list[StoredActiveSetup]: ...
async def save_critic_report(self, report: StoredCriticReport) -> None: ...
```

Import `Resolution` in `store.py` with `from src.agents.critic.models import Resolution`. Verify no import cycle: `python -c "import src.persistence.store; import src.agents.critic.models"`. If a cycle appears, move the import under `if TYPE_CHECKING:` and quote the annotation.

Pagination contract (used by Tasks 6, 7, 12, 13): `after` is the `(created_at, id)` (or `(started_at, id)` / `(opened_at, id)`) of the last row of the previous page; rows are ordered by that tuple ascending; when `after` is given add `AND (ts_col, id) > ($ts, $id)`.

Queue semantics (used by Tasks 6, 7, 8, 11):

- *Unresolved decision* = `signals s LEFT JOIN decision_resolutions r ON r.signal_id = s.id WHERE r.signal_id IS NULL AND (s.direction IS NOT NULL OR s.payload->>'violated_rule' IS NOT NULL) AND s.created_at < :decided_before ORDER BY s.created_at ASC, s.id ASC`.
- *Pending review* = UNION of (a) signals with a resolution row and no review row, or a review row with `error IS NOT NULL AND attempts < 2 AND created_at < :retry_before`; and (b) NO_SETUP signals (`direction IS NULL AND payload->>'violated_rule' IS NULL`) with `created_at < :no_setup_before` and the same review-row condition. Ordered by `created_at ASC, id ASC`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_migrate.py` change the line asserting the table set to:

```python
        assert (
            frozenset(
                {
                    "scan_runs",
                    "signals",
                    "agent_runs",
                    "active_setups",
                    "decision_resolutions",
                    "decision_reviews",
                    "critic_reports",
                }
            )
            == EXPECTED_TABLES
        )
```

```python
# tests/unit/test_persistence_models.py
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.common.models import SignalStatus
from src.persistence.models import DecisionSummary, StoredCriticReport, StoredReview

NOW = datetime(2026, 7, 1, tzinfo=UTC)


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_migrate.py tests/unit/test_persistence_models.py -q`
Expected: FAIL (table-set mismatch; ImportError for the new models).

- [ ] **Step 3: Implement**

Append to `src/persistence/schema.sql`:

```sql
-- ---------------------------------------------------------------------------
-- Critic v0 (design 2026-09-22 §8). decision_resolutions: deterministic price
-- replay per decision -- the journal's ground truth. decision_reviews: Claude's
-- graded judgement per decision. critic_reports: one aggregate report per run.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_resolutions (
    signal_id                   UUID        PRIMARY KEY
                                            REFERENCES signals(id) ON DELETE CASCADE,
    decision_kind               TEXT        NOT NULL
                                            CHECK (decision_kind IN ('PUBLISHED', 'VETOED', 'GATE_REJECTED')),
    outcome                     TEXT        NOT NULL
                                            CHECK (outcome IN ('WIN', 'LOSS', 'EXPIRED', 'UNRESOLVABLE')),
    decided_at                  TIMESTAMPTZ NOT NULL,
    entered_at                  TIMESTAMPTZ,
    resolved_at                 TIMESTAMPTZ NOT NULL,
    realized_r                  DOUBLE PRECISION NOT NULL,
    max_adverse_excursion_r     DOUBLE PRECISION,
    max_favorable_excursion_r   DOUBLE PRECISION,
    tp2_reached                 BOOLEAN     NOT NULL DEFAULT FALSE,
    candles_used                INTEGER     NOT NULL CHECK (candles_used >= 0),
    engine_version              TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_decision_resolutions_outcome
    ON decision_resolutions (outcome);

CREATE TABLE IF NOT EXISTS decision_reviews (
    signal_id       UUID        PRIMARY KEY
                                REFERENCES signals(id) ON DELETE CASCADE,
    decision_kind   TEXT        NOT NULL
                                CHECK (decision_kind IN ('PUBLISHED', 'VETOED', 'GATE_REJECTED', 'NO_SETUP')),
    review          JSONB       NOT NULL DEFAULT '{}'::JSONB,
    model           TEXT        NOT NULL,
    token_usage     JSONB       NOT NULL DEFAULT '{}'::JSONB,
    cost_usd        NUMERIC(12, 6),
    error           TEXT,
    attempts        INTEGER     NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS critic_reports (
    id              UUID        PRIMARY KEY,
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    report          JSONB       NOT NULL,
    markdown        TEXT        NOT NULL,
    pr_url          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_critic_reports_created_at
    ON critic_reports (created_at DESC);
```

`src/persistence/__init__.py`: add the three table names to `EXPECTED_TABLES`; export `DecisionSummary`, `StoredReview`, `StoredCriticReport`.

`src/persistence/models.py`: add the three models from the Interfaces block, copying `StoredActiveSetup`'s config and tz validators.

`src/persistence/store.py`: add the Protocol methods (one-line docstrings) to `SignalStore`; add each method to `DataApiSignalStore` and `AsyncpgSignalStore` with body `raise NotImplementedError("implemented in Task 6/7")` so mypy stays green until those tasks land.

- [ ] **Step 4: Run tests and checkpoints**

Run: `python -m pytest -q && python -m ruff check . && python -m ruff format --check . && python -m mypy --strict src/ scripts/`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/persistence/schema.sql src/persistence/__init__.py src/persistence/models.py src/persistence/store.py tests/unit/test_migrate.py tests/unit/test_persistence_models.py
git commit -m "feat(persistence): Critic v0 schema (decision_resolutions, decision_reviews, critic_reports) + store protocol

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: asyncpg implementations (repositories + AsyncpgSignalStore)

**Files:**
- Modify: `src/persistence/repositories.py` (add `DecisionRepository`)
- Modify: `src/persistence/store.py` (`AsyncpgSignalStore`: replace the Task-5 `NotImplementedError` bodies with delegation)
- Test: `tests/unit/test_repositories.py` (append), `tests/unit/test_asyncpg_store.py` (append)

**Interfaces:**
- Consumes: Task 5 Protocol signatures verbatim; `Resolution`, `DecisionSummary`, `StoredReview`, `StoredCriticReport`.
- Produces: `DecisionRepository(conn)` with methods named exactly like the Protocol methods (same kwargs). `AsyncpgSignalStore.<method>` = `async with self._acquire() as conn: return await DecisionRepository(conn).<method>(...)`.

SQL (asyncpg positional params). Reuse `SignalRepository._record_to_stored` (or its module-level equivalent) for `signals` rows and `AgentRunRepository`'s row mapper for `agent_runs` rows — read those repositories first and call the same mappers rather than re-implementing JSONB parsing. `_SIGNAL_COLUMNS`-style column list: use whatever constant `SignalRepository` selects (grep `SELECT` in `SignalRepository.get_by_id`).

```sql
-- list_unresolved_decisions($1 decided_before, $2 limit)
SELECT s.<signal columns>
FROM signals s
LEFT JOIN decision_resolutions r ON r.signal_id = s.id
WHERE r.signal_id IS NULL
  AND (s.direction IS NOT NULL OR s.payload->>'violated_rule' IS NOT NULL)
  AND s.created_at < $1
ORDER BY s.created_at ASC, s.id ASC
LIMIT $2

-- count_unresolved_decisions($1): same WHERE, SELECT count(*)

-- list_agent_runs_for_scan($1)
SELECT <agent_runs columns> FROM agent_runs WHERE scan_id = $1 ORDER BY created_at ASC

-- save_resolution
INSERT INTO decision_resolutions
  (signal_id, decision_kind, outcome, decided_at, entered_at, resolved_at, realized_r,
   max_adverse_excursion_r, max_favorable_excursion_r, tp2_reached, candles_used, engine_version)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
ON CONFLICT (signal_id) DO UPDATE SET
  decision_kind = EXCLUDED.decision_kind, outcome = EXCLUDED.outcome,
  decided_at = EXCLUDED.decided_at, entered_at = EXCLUDED.entered_at,
  resolved_at = EXCLUDED.resolved_at, realized_r = EXCLUDED.realized_r,
  max_adverse_excursion_r = EXCLUDED.max_adverse_excursion_r,
  max_favorable_excursion_r = EXCLUDED.max_favorable_excursion_r,
  tp2_reached = EXCLUDED.tp2_reached, candles_used = EXCLUDED.candles_used,
  engine_version = EXCLUDED.engine_version, created_at = NOW()

-- get_resolution($1)
SELECT signal_id, decision_kind, outcome, decided_at, entered_at, resolved_at, realized_r,
       max_adverse_excursion_r, max_favorable_excursion_r, tp2_reached, candles_used, engine_version
FROM decision_resolutions WHERE signal_id = $1

-- close_active_setup_for_signal($1 signal_id, $2 status, $3 evaluation jsonb text)
UPDATE active_setups
SET status = $2, latest_evaluation = $3::jsonb, last_evaluated_at = NOW()
WHERE signal_id = $1 AND status = 'OPEN'

-- list_pending_reviews($1 no_setup_before, $2 retry_before, $3 limit)
SELECT * FROM (
  SELECT s.<signal columns>
  FROM signals s
  JOIN decision_resolutions r ON r.signal_id = s.id
  LEFT JOIN decision_reviews v ON v.signal_id = s.id
  WHERE v.signal_id IS NULL
     OR (v.error IS NOT NULL AND v.attempts < 2 AND v.created_at < $2)
  UNION ALL
  SELECT s.<signal columns>
  FROM signals s
  LEFT JOIN decision_reviews v ON v.signal_id = s.id
  WHERE s.direction IS NULL AND s.payload->>'violated_rule' IS NULL
    AND s.created_at < $1
    AND (v.signal_id IS NULL
         OR (v.error IS NOT NULL AND v.attempts < 2 AND v.created_at < $2))
) q
ORDER BY q.created_at ASC, q.id ASC
LIMIT $3

-- count_pending_reviews($1, $2): SELECT count(*) FROM ( <same union> ) q

-- save_review($1 signal_id, $2 decision_kind, $3 review jsonb text, $4 model, $5 token_usage jsonb text, $6 cost_usd, $7 error)
INSERT INTO decision_reviews (signal_id, decision_kind, review, model, token_usage, cost_usd, error, attempts)
VALUES ($1, $2, $3::jsonb, $4, $5::jsonb, $6, $7, 1)
ON CONFLICT (signal_id) DO UPDATE SET
  decision_kind = EXCLUDED.decision_kind, review = EXCLUDED.review, model = EXCLUDED.model,
  token_usage = EXCLUDED.token_usage, cost_usd = EXCLUDED.cost_usd, error = EXCLUDED.error,
  attempts = decision_reviews.attempts + 1, created_at = NOW()

-- list_decision_summaries($1 since, $2 until, $3 after_ts, $4 after_id, $5 limit)
SELECT s.id, s.scan_id, s.symbol, s.direction, s.status, s.created_at,
       s.payload->>'violated_rule' AS violated_rule, s.outcome,
       r.decision_kind, r.outcome AS resolution_outcome, r.realized_r, r.entered_at,
       CASE WHEN v.error IS NULL THEN v.review END AS review, v.error AS review_error
FROM signals s
LEFT JOIN decision_resolutions r ON r.signal_id = s.id
LEFT JOIN decision_reviews v ON v.signal_id = s.id
WHERE s.created_at >= $1 AND s.created_at < $2
  AND ($3::timestamptz IS NULL OR (s.created_at, s.id) > ($3, $4))
ORDER BY s.created_at ASC, s.id ASC
LIMIT $5

-- list_scan_runs_between($1 since, $2 until, $3 after_ts, $4 after_id, $5 limit)
SELECT id, started_at, completed_at, status, error_message, session, strategy, symbols
FROM scan_runs
WHERE started_at >= $1 AND started_at < $2
  AND ($3::timestamptz IS NULL OR (started_at, id) > ($3, $4))
ORDER BY started_at ASC, id ASC
LIMIT $5

-- list_active_setups($1 after_ts, $2 after_id, $3 limit)
SELECT id, signal_id, opened_at, status, last_evaluated_at, latest_evaluation
FROM active_setups
WHERE ($1::timestamptz IS NULL OR (opened_at, id) > ($1, $2))
ORDER BY opened_at ASC, id ASC
LIMIT $3

-- save_critic_report
INSERT INTO critic_reports (id, period_start, period_end, report, markdown, pr_url, created_at)
VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
```

`after=None` → pass `None, None` for the two cursor params. `DecisionSummary.review` must be parsed with `_parse_jsonb_field` when not None; `realized_r` may come back as `float`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_repositories.py`, using that file's existing fake-connection helpers (read its top 100 lines; it has a recording `conn` with `execute`/`fetch`/`fetchrow` AsyncMocks):

```python
class TestDecisionRepository:
    async def test_list_unresolved_binds_cutoff_and_limit(self) -> None:
        conn = _conn()  # the file's fake connection factory
        repo = DecisionRepository(conn)
        cutoff = datetime(2026, 7, 1, tzinfo=UTC)
        await repo.list_unresolved_decisions(limit=25, decided_before=cutoff)
        sql, *args = conn.fetch.await_args.args
        assert "LEFT JOIN decision_resolutions" in sql
        assert "payload->>'violated_rule' IS NOT NULL" in sql
        assert args == [cutoff, 25]

    async def test_save_resolution_upserts_all_columns(self) -> None:
        conn = _conn()
        res = Resolution(
            signal_id=uuid4(), decision_kind=DecisionKind.VETOED, outcome=ResolutionOutcome.WIN,
            decided_at=datetime(2026, 7, 1, tzinfo=UTC), entered_at=None,
            resolved_at=datetime(2026, 7, 2, tzinfo=UTC), realized_r=3.0,
            max_adverse_excursion_r=-0.2, max_favorable_excursion_r=3.1, tp2_reached=True,
            candles_used=10, engine_version="replay-v1",
        )
        await DecisionRepository(conn).save_resolution(res)
        sql, *args = conn.execute.await_args.args
        assert "ON CONFLICT (signal_id) DO UPDATE" in sql
        assert args[0] == res.signal_id and args[1] == "VETOED" and args[2] == "WIN"
        assert args[11] == "replay-v1"

    async def test_save_review_increments_attempts_on_conflict(self) -> None:
        conn = _conn()
        await DecisionRepository(conn).save_review(
            signal_id=uuid4(), decision_kind="PUBLISHED", review={"lesson": "x"}, model="m",
            token_usage={"in": 1}, cost_usd=0.01, error=None,
        )
        sql, *args = conn.execute.await_args.args
        assert "attempts = decision_reviews.attempts + 1" in sql
        assert json.loads(args[2]) == {"lesson": "x"}

    async def test_list_pending_reviews_unions_no_setup_rows(self) -> None:
        conn = _conn()
        t = datetime(2026, 7, 1, tzinfo=UTC)
        await DecisionRepository(conn).list_pending_reviews(limit=5, no_setup_before=t, retry_before=t)
        sql, *args = conn.fetch.await_args.args
        assert "UNION ALL" in sql and "attempts < 2" in sql
        assert args == [t, t, 5]

    async def test_list_decision_summaries_maps_row(self) -> None:
        conn = _conn()
        sid, scan = uuid4(), uuid4()
        conn.fetch = AsyncMock(return_value=[{
            "id": sid, "scan_id": scan, "symbol": "BTCUSDT", "direction": "LONG",
            "status": "PUBLISHED", "created_at": datetime(2026, 7, 1, tzinfo=UTC),
            "violated_rule": None, "outcome": "WIN", "decision_kind": "PUBLISHED",
            "resolution_outcome": "WIN", "realized_r": 3.0,
            "entered_at": datetime(2026, 7, 1, 1, tzinfo=UTC),
            "review": '{"lesson": "ok"}', "review_error": None,
        }])
        t = datetime(2026, 7, 1, tzinfo=UTC)
        rows = await DecisionRepository(conn).list_decision_summaries(since=t, until=t, after=None, limit=10)
        assert rows[0].review == {"lesson": "ok"} and rows[0].realized_r == 3.0
        sql, *args = conn.fetch.await_args.args
        assert args[2] is None and args[3] is None and args[4] == 10

    async def test_close_active_setup_for_signal_only_open_rows(self) -> None:
        conn = _conn()
        await DecisionRepository(conn).close_active_setup_for_signal(
            signal_id=uuid4(), status=ActiveSetupStatus.WIN, evaluation={"source": "resolver"}
        )
        sql, *_ = conn.execute.await_args.args
        assert "status = 'OPEN'" in sql and "signal_id = $1" in sql
```

Append to `tests/unit/test_asyncpg_store.py` one wiring test per new store method asserting the call reaches the pooled connection (pattern: `store, conn = _store(); await store.count_unresolved_decisions(decided_before=...); conn.fetchval.assert_awaited()` — add `conn.fetchval = AsyncMock(return_value=0)` to `_conn()` if missing).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_repositories.py tests/unit/test_asyncpg_store.py -q`
Expected: FAIL — `ImportError: cannot import name 'DecisionRepository'` / `NotImplementedError`.

- [ ] **Step 3: Implement**

Add `DecisionRepository` to `src/persistence/repositories.py` with the SQL above; counts use `conn.fetchval`. Map rows: `Resolution.model_validate(dict(record))`, `DecisionSummary.model_validate({... "review": _parse_jsonb_field(r["review"]) if r["review"] is not None else None})`, reuse existing mappers for signals / agent_runs / scan_runs / active_setups (import the row-mapping staticmethods or call the sibling repositories' mappers). Then replace each `NotImplementedError` in `AsyncpgSignalStore` with the `_acquire()` delegation.

- [ ] **Step 4: Run tests and checkpoints**

Run: `python -m pytest -q && python -m ruff check . && python -m ruff format --check . && python -m mypy --strict src/ scripts/`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/persistence/repositories.py src/persistence/store.py tests/unit/test_repositories.py tests/unit/test_asyncpg_store.py
git commit -m "feat(persistence): asyncpg DecisionRepository + store wiring for Critic v0

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: RDS Data API implementations (DataApiSignalStore)

**Files:**
- Modify: `src/persistence/store.py` (`DataApiSignalStore`: replace the Task-5 `NotImplementedError` bodies)
- Test: `tests/unit/test_data_api_store.py` (append)

**Interfaces:**
- Consumes: Task 5 Protocol signatures; helpers `_str_param`, `_long_param`, `_double_param`, `_utc_iso`, `_parse_records`, `_parse_jsonb_field`, `_to_jsonb`, `self._execute(sql, params, with_metadata=True)`.
- Produces: the same behaviour as Task 6 over the Data API.

Translate every Task-6 statement to named params with casts (`:signal_id::uuid`, `:decided_before::timestamptz`, `:review::jsonb`, `:limit` as `_long_param`, doubles via `_double_param`). Booleans: add a helper

```python
def _bool_param(name: str, value: bool | None) -> dict[str, Any]:
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    return {"name": name, "value": {"booleanValue": value}}
```

Select timestamps through `_utc_iso(col, alias=...)`, uuids as `id::text AS id`, JSONB as `col::text AS col`, then `Model.model_validate(row)` after `_parse_jsonb_field` on JSONB text. For `list_decision_summaries` the review column is `CASE WHEN v.error IS NULL THEN v.review::text END AS review`. Cursor params: `(:after_ts::timestamptz IS NULL OR (s.created_at, s.id) > (:after_ts::timestamptz, :after_id::uuid))`.

`count_*` methods: `SELECT count(*) AS n ...`, `with_metadata=True`, return `int(_parse_records(response)[0]["n"])`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_data_api_store.py` (reuse `make_store`, `params_by_name`, `response(columns, *records)`):

```python
async def test_list_unresolved_decisions_binds_cutoff_and_limit() -> None:
    store, client = make_store(response([], ))
    cutoff = datetime(2026, 7, 1, tzinfo=UTC)
    await store.list_unresolved_decisions(limit=25, decided_before=cutoff)
    call = client.calls[0]
    assert "LEFT JOIN decision_resolutions" in call["sql"]
    p = params_by_name(call)
    assert p["decided_before"] == {"stringValue": cutoff.isoformat()}
    assert p["limit"] == {"longValue": 25}


async def test_count_unresolved_decisions_parses_count() -> None:
    store, _ = make_store(response(["n"], [{"longValue": 7}]))
    assert await store.count_unresolved_decisions(decided_before=datetime(2026, 7, 1, tzinfo=UTC)) == 7


async def test_save_resolution_types_every_param() -> None:
    store, client = make_store()
    res = Resolution(
        signal_id=uuid4(), decision_kind=DecisionKind.PUBLISHED, outcome=ResolutionOutcome.LOSS,
        decided_at=datetime(2026, 7, 1, tzinfo=UTC), entered_at=None,
        resolved_at=datetime(2026, 7, 2, tzinfo=UTC), realized_r=-1.0,
        max_adverse_excursion_r=-1.0, max_favorable_excursion_r=0.4, tp2_reached=False,
        candles_used=12, engine_version="replay-v1",
    )
    await store.save_resolution(res)
    p = params_by_name(client.calls[0])
    assert p["signal_id"] == {"stringValue": str(res.signal_id)}
    assert p["entered_at"] == {"isNull": True}
    assert p["realized_r"] == {"doubleValue": -1.0}
    assert p["tp2_reached"] == {"booleanValue": False}
    assert p["candles_used"] == {"longValue": 12}
    assert "ON CONFLICT (signal_id) DO UPDATE" in client.calls[0]["sql"]


async def test_get_resolution_parses_row() -> None:
    sid = uuid4()
    store, _ = make_store(response(
        ["signal_id", "decision_kind", "outcome", "decided_at", "entered_at", "resolved_at",
         "realized_r", "max_adverse_excursion_r", "max_favorable_excursion_r", "tp2_reached",
         "candles_used", "engine_version"],
        [{"stringValue": str(sid)}, {"stringValue": "VETOED"}, {"stringValue": "WIN"},
         {"stringValue": "2026-07-01T00:00:00.000000+00:00"}, {"isNull": True},
         {"stringValue": "2026-07-02T00:00:00.000000+00:00"}, {"doubleValue": 3.0},
         {"isNull": True}, {"isNull": True}, {"booleanValue": True}, {"longValue": 5},
         {"stringValue": "replay-v1"}],
    ))
    res = await store.get_resolution(sid)
    assert res is not None and res.outcome is ResolutionOutcome.WIN and res.tp2_reached is True


async def test_save_review_uses_jsonb_casts_and_attempt_increment() -> None:
    store, client = make_store()
    await store.save_review(
        signal_id=uuid4(), decision_kind="NO_SETUP", review={"lesson": "x"}, model="m",
        token_usage={}, cost_usd=None, error="boom",
    )
    call = client.calls[0]
    assert ":review::jsonb" in call["sql"] and "attempts = decision_reviews.attempts + 1" in call["sql"]
    p = params_by_name(call)
    assert json.loads(p["review"]["stringValue"]) == {"lesson": "x"}
    assert p["cost_usd"] == {"isNull": True}
    assert p["error"] == {"stringValue": "boom"}


async def test_list_pending_reviews_binds_both_cutoffs() -> None:
    store, client = make_store(response([]))
    t = datetime(2026, 7, 1, tzinfo=UTC)
    await store.list_pending_reviews(limit=3, no_setup_before=t, retry_before=t)
    call = client.calls[0]
    assert "UNION ALL" in call["sql"]
    p = params_by_name(call)
    assert {"no_setup_before", "retry_before", "limit"} <= set(p)


async def test_list_decision_summaries_first_page_nulls_cursor() -> None:
    store, client = make_store(response([]))
    t = datetime(2026, 7, 1, tzinfo=UTC)
    await store.list_decision_summaries(since=t, until=t, after=None, limit=500)
    p = params_by_name(client.calls[0])
    assert p["after_ts"] == {"isNull": True} and p["after_id"] == {"isNull": True}


async def test_list_decision_summaries_parses_review_json() -> None:
    sid, scan = uuid4(), uuid4()
    cols = ["id", "scan_id", "symbol", "direction", "status", "created_at", "violated_rule",
            "outcome", "decision_kind", "resolution_outcome", "realized_r", "entered_at",
            "review", "review_error"]
    rec = [{"stringValue": str(sid)}, {"stringValue": str(scan)}, {"stringValue": "ETHUSDT"},
           {"isNull": True}, {"stringValue": "SKIPPED"},
           {"stringValue": "2026-07-01T08:03:00.000000+00:00"}, {"stringValue": "RULE_2_MIN_RR"},
           {"isNull": True}, {"stringValue": "GATE_REJECTED"}, {"stringValue": "LOSS"},
           {"doubleValue": -1.0}, {"isNull": True}, {"stringValue": '{"gate_correct": true}'},
           {"isNull": True}]
    store, _ = make_store(response(cols, rec))
    t = datetime(2026, 7, 1, tzinfo=UTC)
    rows = await store.list_decision_summaries(since=t, until=t, after=None, limit=10)
    assert rows[0].review == {"gate_correct": True} and rows[0].violated_rule == "RULE_2_MIN_RR"


async def test_list_scan_runs_between_second_page_binds_cursor() -> None:
    store, client = make_store(response([]))
    t = datetime(2026, 7, 1, tzinfo=UTC)
    cur = uuid4()
    await store.list_scan_runs_between(since=t, until=t, after=(t, cur), limit=100)
    p = params_by_name(client.calls[0])
    assert p["after_id"] == {"stringValue": str(cur)}


async def test_close_active_setup_for_signal() -> None:
    store, client = make_store()
    await store.close_active_setup_for_signal(
        signal_id=uuid4(), status=ActiveSetupStatus.LOSS, evaluation={"source": "resolver"}
    )
    call = client.calls[0]
    assert "status = 'OPEN'" in call["sql"]
    assert params_by_name(call)["status"] == {"stringValue": "LOSS"}


async def test_save_critic_report() -> None:
    store, client = make_store()
    rep = StoredCriticReport(
        id=uuid4(), period_start=datetime(2026, 7, 1, tzinfo=UTC),
        period_end=datetime(2026, 7, 8, tzinfo=UTC), report={"summary": "s"}, markdown="# m",
        pr_url=None, created_at=datetime(2026, 7, 8, tzinfo=UTC),
    )
    await store.save_critic_report(rep)
    p = params_by_name(client.calls[0])
    assert p["pr_url"] == {"isNull": True} and json.loads(p["report"]["stringValue"]) == {"summary": "s"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_data_api_store.py -q -k "unresolved or resolution or review or summaries or scan_runs_between or close_active or critic_report"`
Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Implement** the methods in `DataApiSignalStore` per the translation rules above, replacing every Task-5 stub.

- [ ] **Step 4: Run tests and checkpoints**

Run: `python -m pytest -q && python -m ruff check . && python -m ruff format --check . && python -m mypy --strict src/ scripts/`
Expected: all green; `grep -n NotImplementedError src/persistence/store.py` prints nothing.

- [ ] **Step 5: Commit**

```bash
git add src/persistence/store.py tests/unit/test_data_api_store.py
git commit -m "feat(persistence): Data API implementations of the Critic v0 store methods

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: OutcomeResolver

**Files:**
- Create: `src/agents/critic/resolver.py`
- Test: `tests/unit/test_critic_resolver.py`

**Interfaces:**
- Consumes: `SignalStore` (Tasks 5–7), `DataProvider.fetch_klines` (Task 4), `classify`/`is_scoreable` (Task 3), `replay`, `ENGINE_VERSION`, `MAX_HOLD` (Task 2), `Resolution`, `ResolutionOutcome`, `DecisionKind` (Task 1), `Timeframe`, `ProviderError` (`src/providers.base`), `SignalOutcome`, `ActiveSetupStatus` (`src/common.models`).
- Produces:

```python
@dataclass(frozen=True)
class ResolveSummary:
    resolved: int
    deferred: int      # window still open OR provider error -> retried next run
    remaining: int     # closable unresolved decisions left after this run

class OutcomeResolver:
    def __init__(self, *, store: SignalStore, provider: DataProvider, now: Callable[[], datetime] = _utcnow) -> None
    async def run(self, *, chunk_size: int = 25) -> ResolveSummary
    async def resolve_one(self, signal: StoredSignal) -> Resolution | None   # None == deferred
```

Behaviour:
1. `decided_before = now() - MAX_HOLD`. Queue = `store.list_unresolved_decisions(limit=chunk_size, decided_before=now())` — note: the *list* uses `now()` so freshly closable and still-open decisions both get a chance (pending ones are cheap to defer), while `remaining` is `store.count_unresolved_decisions(decided_before=decided_before)` so the backfill loop terminates.
2. For each signal: `runs = await store.list_agent_runs_for_scan(signal.scan_id)`; `record = classify(signal, runs)`. If not `is_scoreable(record.kind)`: skip (cannot happen given the query; log at debug). If `record.proposal is None`: write `Resolution(outcome=UNRESOLVABLE, decided_at=signal.created_at, resolved_at=now(), realized_r=0.0, candles_used=0, engine_version=ENGINE_VERSION)` and count as resolved.
3. Else fetch candles: `start = signal.created_at`, `end = min(now(), start + MAX_HOLD)`; candles are cached in `self._cache: dict[tuple[str, datetime, datetime], list[Kline]]` keyed by `(symbol, start_day_floor, end)` where `start_day_floor = start.replace(hour=0, minute=0, second=0, microsecond=0)` — fetch from `start_day_floor` so decisions on the same symbol/day share one fetch. On `ProviderError`: `logger.warning("PROVIDER_ERROR provider=%s symbol=%s error=%s: %s", ...)` (same marker the scan uses so the existing alarm counts it) and defer.
4. `result = replay(proposal=record.proposal, decided_at=signal.created_at, candles=candles, now=now())`. `result.outcome is None` → defer. Else build `Resolution` from the result (`decision_kind=record.kind`, `resolved_at=result.resolved_at or now()`), `store.save_resolution(res)`.
5. If `record.kind is DecisionKind.PUBLISHED` and outcome ∈ {WIN, LOSS, EXPIRED}: `store.set_signal_outcome(signal_id=signal.id, outcome=SignalOutcome(res.outcome.value), outcome_metadata={"source": "resolver", **res.model_dump(mode="json")})` and `store.close_active_setup_for_signal(signal_id=signal.id, status=ActiveSetupStatus(res.outcome.value), evaluation={"source": "resolver", "outcome": res.outcome.value, "realized_r": res.realized_r})`. VETOED / GATE_REJECTED never touch `signals.outcome` (design §5.4).
6. Per-decision exceptions other than `ProviderError` are logged with `logger.exception` and counted as deferred; the batch never raises.
7. Log one summary line: `"resolver run complete: resolved=%d deferred=%d remaining=%d"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_critic_resolver.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from unittest.mock import AsyncMock, MagicMock

from src.agents.critic.models import DecisionKind, Resolution, ResolutionOutcome
from src.agents.critic.replay import MAX_HOLD
from src.agents.critic.resolver import OutcomeResolver, ResolveSummary
from src.common.models import ActiveSetupStatus, AgentRole, SignalDirection, SignalOutcome, SignalStatus
from src.persistence.models import StoredAgentRun, StoredSignal
from src.providers.base import Kline, ProviderError, Timeframe

T0 = datetime(2026, 7, 1, 8, 3, tzinfo=UTC)
NOW = T0 + MAX_HOLD + timedelta(days=1)


def _proposal_payload(scan_id: UUID, **over: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "proposal_id": str(uuid4()), "scan_id": str(scan_id), "strategy": "smc",
        "created_at": T0.isoformat(), "symbol": "BTCUSDT", "direction": "LONG",
        "entry_price": 100.0, "stop_loss": 98.0, "take_profit_1": 106.0, "take_profit_2": None,
        "risk_reward_ratio": 3.0, "leverage": 3.0, "risk_percent": 1.0, "tags": [],
        "confluence_narrative": "t", "features": {},
    }
    p.update(over)
    return p


def _signal(payload: dict[str, Any], scan_id: UUID) -> StoredSignal:
    has_dir = "direction" in payload
    return StoredSignal(
        id=uuid4(), scan_id=scan_id, symbol="BTCUSDT", strategy="smc",
        direction=SignalDirection.LONG if has_dir else None,
        status=SignalStatus.PUBLISHED if has_dir else SignalStatus.SKIPPED,
        created_at=T0, payload=payload, tags=[], features={},
    )


def _judge(scan_id: UUID, ruling: str) -> StoredAgentRun:
    return StoredAgentRun(
        id=uuid4(), scan_id=scan_id, agent_role=AgentRole.JUDGE, strategy="smc", input_hash="h",
        output={"ruling": ruling}, latency_ms=0, token_usage={}, cost_usd=None, created_at=T0,
    )


def _candles(*rows: tuple[int, float, float, float, float]) -> list[Kline]:
    return [
        Kline(open_time=T0 + timedelta(minutes=5 * i), open=o, high=h, low=lo, close=c, volume=1.0)
        for i, o, h, lo, c in rows
    ]


class FakeStore:
    def __init__(self, signals: list[StoredSignal], runs: dict[UUID, list[StoredAgentRun]], remaining: int = 0) -> None:
        self._signals = signals
        self._runs = runs
        self._remaining = remaining
        self.saved: list[Resolution] = []
        self.outcomes: list[tuple[UUID, SignalOutcome]] = []
        self.closed: list[tuple[UUID, ActiveSetupStatus]] = []

    async def list_unresolved_decisions(self, *, limit: int, decided_before: datetime) -> list[StoredSignal]:
        return self._signals[:limit]

    async def count_unresolved_decisions(self, *, decided_before: datetime) -> int:
        return self._remaining

    async def list_agent_runs_for_scan(self, scan_id: UUID) -> list[StoredAgentRun]:
        return self._runs.get(scan_id, [])

    async def save_resolution(self, resolution: Resolution) -> None:
        self.saved.append(resolution)

    async def set_signal_outcome(self, *, signal_id: UUID, outcome: SignalOutcome, outcome_metadata: Any = None) -> None:
        self.outcomes.append((signal_id, outcome))

    async def close_active_setup_for_signal(self, *, signal_id: UUID, status: ActiveSetupStatus, evaluation: Any) -> None:
        self.closed.append((signal_id, status))


def _provider(candles: list[Kline] | Exception) -> MagicMock:
    p = MagicMock()
    p.fetch_klines = AsyncMock(side_effect=candles) if isinstance(candles, Exception) else AsyncMock(return_value=candles)
    return p


async def test_published_win_writes_resolution_outcome_and_closes_setup() -> None:
    scan = uuid4()
    sig = _signal(_proposal_payload(scan), scan)
    store = FakeStore([sig], {scan: [_judge(scan, "PUBLISH")]})
    provider = _provider(_candles((0, 100, 100.5, 99.9, 100.2), (1, 100.2, 106.5, 100.1, 106)))
    summary = await OutcomeResolver(store=store, provider=provider, now=lambda: NOW).run(chunk_size=25)
    assert summary == ResolveSummary(resolved=1, deferred=0, remaining=0)
    res = store.saved[0]
    assert res.decision_kind is DecisionKind.PUBLISHED and res.outcome is ResolutionOutcome.WIN
    assert store.outcomes == [(sig.id, SignalOutcome.WIN)]
    assert store.closed == [(sig.id, ActiveSetupStatus.WIN)]
    provider.fetch_klines.assert_awaited_once()
    kwargs = provider.fetch_klines.await_args.kwargs
    assert kwargs["timeframe"] is Timeframe.M5 if "timeframe" in kwargs else True


async def test_vetoed_loss_does_not_touch_signal_outcome() -> None:
    scan = uuid4()
    sig = _signal(_proposal_payload(scan), scan)
    store = FakeStore([sig], {scan: [_judge(scan, "SKIP")]})
    provider = _provider(_candles((0, 100, 100.5, 99.9, 100.2), (1, 100.2, 100.4, 97.5, 98)))
    await OutcomeResolver(store=store, provider=provider, now=lambda: NOW).run()
    assert store.saved[0].decision_kind is DecisionKind.VETOED
    assert store.saved[0].outcome is ResolutionOutcome.LOSS
    assert store.outcomes == [] and store.closed == []


async def test_pending_window_is_deferred_not_saved() -> None:
    scan = uuid4()
    sig = _signal(_proposal_payload(scan), scan)
    store = FakeStore([sig], {scan: [_judge(scan, "PUBLISH")]}, remaining=0)
    provider = _provider(_candles((0, 100, 100.5, 99.9, 100.2)))
    summary = await OutcomeResolver(store=store, provider=provider, now=lambda: T0 + timedelta(hours=1)).run()
    assert summary.deferred == 1 and store.saved == []


async def test_provider_error_defers_and_continues_batch() -> None:
    scan1, scan2 = uuid4(), uuid4()
    s1, s2 = _signal(_proposal_payload(scan1), scan1), _signal(_proposal_payload(scan2), scan2)
    store = FakeStore([s1, s2], {scan1: [_judge(scan1, "PUBLISH")], scan2: [_judge(scan2, "PUBLISH")]})
    good = _candles((0, 100, 100.5, 99.9, 100.2), (1, 100.2, 106.5, 100.1, 106))
    provider = MagicMock()
    provider.fetch_klines = AsyncMock(side_effect=[ProviderError("451", provider="binance"), good])
    summary = await OutcomeResolver(store=store, provider=provider, now=lambda: NOW).run()
    assert summary.resolved == 1 and summary.deferred == 1


async def test_gate_rejected_without_proposal_is_unresolvable() -> None:
    scan = uuid4()
    skip = {"decision_id": str(uuid4()), "scan_id": str(scan), "strategy": "smc", "symbol": "BTCUSDT",
            "reason": "INSUFFICIENT_RR", "details": "gate rejected", "violated_rule": "RULE_2_MIN_RR",
            "created_at": T0.isoformat()}
    store = FakeStore([_signal(skip, scan)], {})
    provider = _provider([])
    summary = await OutcomeResolver(store=store, provider=provider, now=lambda: NOW).run()
    assert summary.resolved == 1
    assert store.saved[0].outcome is ResolutionOutcome.UNRESOLVABLE
    provider.fetch_klines.assert_not_awaited()


async def test_same_symbol_same_day_shares_one_fetch() -> None:
    scan1, scan2 = uuid4(), uuid4()
    s1 = _signal(_proposal_payload(scan1), scan1)
    s2 = _signal(_proposal_payload(scan2), scan2)
    store = FakeStore([s1, s2], {scan1: [_judge(scan1, "PUBLISH")], scan2: [_judge(scan2, "PUBLISH")]})
    provider = _provider(_candles((0, 100, 100.5, 99.9, 100.2), (1, 100.2, 106.5, 100.1, 106)))
    await OutcomeResolver(store=store, provider=provider, now=lambda: NOW).run()
    assert provider.fetch_klines.await_count == 1


async def test_remaining_comes_from_store_count() -> None:
    store = FakeStore([], {}, remaining=42)
    summary = await OutcomeResolver(store=store, provider=_provider([]), now=lambda: NOW).run()
    assert summary == ResolveSummary(resolved=0, deferred=0, remaining=42)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_critic_resolver.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/agents/critic/resolver.py`**

```python
"""OutcomeResolver: deterministic ground truth for every scoreable decision (design §5)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from src.agents.critic.inventory import classify, is_scoreable
from src.agents.critic.models import DecisionKind, DecisionRecord, Resolution, ResolutionOutcome
from src.agents.critic.replay import ENGINE_VERSION, MAX_HOLD, replay
from src.common.models import ActiveSetupStatus, SignalOutcome
from src.persistence.models import StoredSignal
from src.persistence.store import SignalStore
from src.providers.base import DataProvider, Kline, ProviderError, Timeframe

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({ResolutionOutcome.WIN, ResolutionOutcome.LOSS, ResolutionOutcome.EXPIRED})


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ResolveSummary:
    resolved: int
    deferred: int
    remaining: int


class OutcomeResolver:
    def __init__(
        self,
        *,
        store: SignalStore,
        provider: DataProvider,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._provider = provider
        self._now = now
        self._cache: dict[tuple[str, datetime, datetime], list[Kline]] = {}

    async def run(self, *, chunk_size: int = 25) -> ResolveSummary:
        now = self._now()
        signals = await self._store.list_unresolved_decisions(limit=chunk_size, decided_before=now)
        resolved = deferred = 0
        for signal in signals:
            try:
                result = await self.resolve_one(signal)
            except ProviderError as exc:
                logger.warning(
                    "PROVIDER_ERROR provider=%s symbol=%s error=%s: %s",
                    type(self._provider).__name__, signal.symbol, type(exc).__name__, exc,
                )
                deferred += 1
                continue
            except Exception:
                logger.exception("resolver failed for signal %s", signal.id)
                deferred += 1
                continue
            if result is None:
                deferred += 1
            else:
                resolved += 1
        remaining = await self._store.count_unresolved_decisions(decided_before=now - MAX_HOLD)
        logger.info(
            "resolver run complete: resolved=%d deferred=%d remaining=%d", resolved, deferred, remaining
        )
        return ResolveSummary(resolved=resolved, deferred=deferred, remaining=remaining)

    async def resolve_one(self, signal: StoredSignal) -> Resolution | None:
        runs = await self._store.list_agent_runs_for_scan(signal.scan_id)
        record = classify(signal, runs)
        if not is_scoreable(record.kind):
            logger.debug("signal %s kind %s is not scoreable", signal.id, record.kind)
            return None
        now = self._now()
        if record.proposal is None:
            res = Resolution(
                signal_id=signal.id, decision_kind=record.kind,
                outcome=ResolutionOutcome.UNRESOLVABLE, decided_at=signal.created_at,
                entered_at=None, resolved_at=now, realized_r=0.0, candles_used=0,
                engine_version=ENGINE_VERSION,
            )
            await self._store.save_resolution(res)
            return res

        candles = await self._candles(signal.symbol, signal.created_at, now)
        result = replay(
            proposal=record.proposal, decided_at=signal.created_at, candles=candles, now=now
        )
        if result.outcome is None:
            return None
        res = Resolution(
            signal_id=signal.id, decision_kind=record.kind, outcome=result.outcome,
            decided_at=signal.created_at, entered_at=result.entered_at,
            resolved_at=result.resolved_at or now, realized_r=result.realized_r,
            max_adverse_excursion_r=result.max_adverse_excursion_r,
            max_favorable_excursion_r=result.max_favorable_excursion_r,
            tp2_reached=result.tp2_reached, candles_used=result.candles_used,
            engine_version=ENGINE_VERSION,
        )
        await self._store.save_resolution(res)
        await self._propagate(record, res)
        return res

    async def _propagate(self, record: DecisionRecord, res: Resolution) -> None:
        if record.kind is not DecisionKind.PUBLISHED or res.outcome not in _TERMINAL:
            return
        meta = {"source": "resolver", **res.model_dump(mode="json")}
        await self._store.set_signal_outcome(
            signal_id=res.signal_id, outcome=SignalOutcome(res.outcome.value), outcome_metadata=meta
        )
        await self._store.close_active_setup_for_signal(
            signal_id=res.signal_id,
            status=ActiveSetupStatus(res.outcome.value),
            evaluation={"source": "resolver", "outcome": res.outcome.value, "realized_r": res.realized_r},
        )

    async def _candles(self, symbol: str, decided_at: datetime, now: datetime) -> list[Kline]:
        start = decided_at.replace(hour=0, minute=0, second=0, microsecond=0)
        end = min(now, decided_at + MAX_HOLD)
        key = (symbol, start, end)
        cached = self._cache.get(key)
        if cached is None:
            cached = await self._provider.fetch_klines(symbol, Timeframe.M5, start=start, end=end)
            self._cache[key] = cached
        return cached
```

- [ ] **Step 4: Run tests and checkpoints**

Run: `python -m pytest tests/unit/test_critic_resolver.py -q && python -m ruff check . && python -m ruff format --check . && python -m mypy --strict src/ scripts/`
Expected: all green. (`ruff format` will re-wrap the long argument lists; run `python -m ruff format src/agents/critic tests/unit/test_critic_resolver.py` and re-check.)

- [ ] **Step 5: Commit**

```bash
git add src/agents/critic/resolver.py tests/unit/test_critic_resolver.py
git commit -m "feat(critic): OutcomeResolver — chunked, idempotent ground-truth resolution

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Lambda `{"mode":"resolve"}` dispatch

**Files:**
- Modify: `scripts/run_scan.py` (add `_run_resolver`, dispatch in `_alambda`, docstring of `lambda_handler`)
- Test: `tests/unit/test_run_scan.py` (append to `TestLambdaHandler`)

**Interfaces:**
- Consumes: `OutcomeResolver`, `ResolveSummary` (Task 8), `create_store`, `BinanceProvider`, `Settings`.
- Produces: event `{"mode": "resolve", "chunk_size": 25}` → `{"ok": True, "mode": "resolve", "resolved": n, "deferred": m, "remaining": k}`. `chunk_size` defaults to 25, clamped to `1..100`.

- [ ] **Step 1: Write the failing test** (append inside `TestLambdaHandler`, mirroring `test_forecaster_mode_runs_forecaster` at ~line 701 — copy its monkeypatching of `create_store` / `BinanceProvider`):

```python
    async def test_resolve_mode_runs_resolver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = MagicMock()
        store.aclose = AsyncMock()
        provider = MagicMock()
        provider.aclose = AsyncMock()
        monkeypatch.setattr(run_scan_module, "create_store", AsyncMock(return_value=store))
        monkeypatch.setattr(run_scan_module, "BinanceProvider", MagicMock(return_value=provider))
        fake_resolver = MagicMock()
        fake_resolver.run = AsyncMock(
            return_value=ResolveSummary(resolved=3, deferred=1, remaining=9)
        )
        resolver_cls = MagicMock(return_value=fake_resolver)
        monkeypatch.setattr(run_scan_module, "OutcomeResolver", resolver_cls)

        result = await _alambda({"mode": "resolve", "chunk_size": 7}, _settings())

        assert result == {"ok": True, "mode": "resolve", "resolved": 3, "deferred": 1, "remaining": 9}
        fake_resolver.run.assert_awaited_once_with(chunk_size=7)
        resolver_cls.assert_called_once_with(store=store, provider=provider)
        store.aclose.assert_awaited_once()
        provider.aclose.assert_awaited_once()

    async def test_resolve_mode_clamps_chunk_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = MagicMock(); store.aclose = AsyncMock()
        provider = MagicMock(); provider.aclose = AsyncMock()
        monkeypatch.setattr(run_scan_module, "create_store", AsyncMock(return_value=store))
        monkeypatch.setattr(run_scan_module, "BinanceProvider", MagicMock(return_value=provider))
        fake_resolver = MagicMock()
        fake_resolver.run = AsyncMock(return_value=ResolveSummary(0, 0, 0))
        monkeypatch.setattr(run_scan_module, "OutcomeResolver", MagicMock(return_value=fake_resolver))
        await _alambda({"mode": "resolve", "chunk_size": 999}, _settings())
        fake_resolver.run.assert_awaited_once_with(chunk_size=100)
```

Add `from src.agents.critic.resolver import ResolveSummary` to the test imports.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_run_scan.py -q -k resolve_mode`
Expected: FAIL — `AttributeError: module 'scripts.run_scan' has no attribute 'OutcomeResolver'`.

- [ ] **Step 3: Implement** in `scripts/run_scan.py`:

```python
from src.agents.critic.resolver import OutcomeResolver

RESOLVE_CHUNK_DEFAULT = 25
RESOLVE_CHUNK_MAX = 100


async def _run_resolver(*, settings: Settings, chunk_size: int) -> dict[str, Any]:
    """Resolve one chunk of decisions against real price action ({"mode": "resolve"}).

    Critic v0 Layer 1 (design §5). Runs in this Lambda because Binance geofences
    GitHub's US runners; the review layer (Actions) reads the rows this writes.
    """
    store = await create_store(settings)
    try:
        provider = BinanceProvider()
        try:
            summary = await OutcomeResolver(store=store, provider=provider).run(
                chunk_size=chunk_size
            )
        finally:
            await provider.aclose()
    finally:
        await store.aclose()
    return {
        "ok": True,
        "mode": "resolve",
        "resolved": summary.resolved,
        "deferred": summary.deferred,
        "remaining": summary.remaining,
    }
```

In `_alambda`, after the forecaster branch:

```python
    # Critic v0 Layer 1: {"mode": "resolve"} replays unresolved decisions against
    # 5m candles and writes decision_resolutions (+ signals.outcome for published).
    if payload.get("mode") == "resolve":
        raw = payload.get("chunk_size", RESOLVE_CHUNK_DEFAULT)
        chunk = max(1, min(RESOLVE_CHUNK_MAX, int(raw)))
        return await _run_resolver(settings=settings, chunk_size=chunk)
```

Extend the `lambda_handler` docstring's mode list with `"resolve"`.

- [ ] **Step 4: Run tests and checkpoints**

Run: `python -m pytest tests/unit/test_run_scan.py -q && python -m ruff check . && python -m ruff format --check . && python -m mypy --strict src/ scripts/`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_scan.py tests/unit/test_run_scan.py
git commit -m "feat(lambda): {\"mode\":\"resolve\"} runs the OutcomeResolver chunk

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Slice 2 exit gate (pure)

**Files:**
- Create: `src/agents/critic/exit_gate.py`
- Test: `tests/unit/test_critic_exit_gate.py`

**Interfaces:**
- Consumes: `StoredScanRun`, `StoredActiveSetup`, `DecisionSummary` (persistence models), `GateCriterion`, `ExitGateReport` (Task 1).
- Produces: `evaluate_exit_gate(*, scan_runs: Sequence[StoredScanRun], decisions: Sequence[DecisionSummary], setups: Sequence[StoredActiveSetup], expected_scans_per_day: int = 16) -> ExitGateReport`.

Criteria (design §6.4, corrected from the validation-plan review):

1. **7 consecutive days autonomous**: group `scan_runs` by UTC date; a day is healthy when it has `>= expected_scans_per_day` rows with `status == SUCCESS` and zero `FAILED`. Pass when the longest run of consecutive healthy dates ≥ 7. Evidence: `"longest healthy streak: N days (2026-07-03..2026-07-09); failed runs: F"`.
2. **≥5 published and ≥5 skipped**: published = decisions whose `id` is in `{s.signal_id for s in setups}`; skipped = decisions with `direction is None` **plus** decisions with a direction that are not in the setup set (Judge vetoes). Evidence: `"published=P skipped=S (analyzer/gate skips=A, judge vetoes=V)"`.
3. **Forecaster updated ≥1 setup**: any setup with `last_evaluated_at is not None` **and** `latest_evaluation` containing a `"status"` key (the Forecaster's `ForecasterUpdate` shape; resolver-written evaluations carry `"source": "resolver"` and must not count). Evidence: count of such setups.
4. **Risk gate prevented ≥1 rule-violating signal**: decisions with `violated_rule is not None`. Evidence: `"gate rejections=N; rules: RULE_2_MIN_RR×3, ..."` plus the fixed note `"CloudWatch cross-check unavailable: risk gates do not log rule ids (finding)."`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_critic_exit_gate.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.agents.critic.exit_gate import evaluate_exit_gate
from src.common.models import ActiveSetupStatus, ScanStatus, SignalDirection, SignalStatus
from src.persistence.models import DecisionSummary, StoredActiveSetup, StoredScanRun

D0 = datetime(2026, 7, 1, 8, 3, tzinfo=UTC)


def _runs(days: int, per_day: int = 16, failed_on: set[int] | None = None) -> list[StoredScanRun]:
    out: list[StoredScanRun] = []
    for d in range(days):
        for i in range(per_day):
            status = ScanStatus.FAILED if failed_on and d in failed_on and i == 0 else ScanStatus.SUCCESS
            out.append(StoredScanRun(id=uuid4(), started_at=D0 + timedelta(days=d, minutes=i), completed_at=None, status=status))
    return out


def _decision(direction: SignalDirection | None, violated: str | None = None) -> DecisionSummary:
    return DecisionSummary(
        id=uuid4(), scan_id=uuid4(), symbol="BTCUSDT", direction=direction,
        status=SignalStatus.PUBLISHED if direction else SignalStatus.SKIPPED, created_at=D0,
        violated_rule=violated, outcome=None, decision_kind=None, resolution_outcome=None,
        realized_r=None, entered_at=None, review=None, review_error=None,
    )


def _setup(signal_id, evaluation=None, evaluated=True) -> StoredActiveSetup:
    return StoredActiveSetup(
        id=uuid4(), signal_id=signal_id, opened_at=D0, status=ActiveSetupStatus.OPEN,
        last_evaluated_at=D0 if evaluated else None, latest_evaluation=evaluation,
    )


def test_all_four_pass() -> None:
    published = [_decision(SignalDirection.LONG) for _ in range(5)]
    vetoed = [_decision(SignalDirection.SHORT) for _ in range(2)]
    skips = [_decision(None) for _ in range(3)] + [_decision(None, "RULE_2_MIN_RR")]
    setups = [_setup(p.id, {"status": "STILL_VALID", "reasoning": "ok"}) for p in published]
    rep = evaluate_exit_gate(scan_runs=_runs(8), decisions=published + vetoed + skips, setups=setups)
    assert rep.all_passed
    assert "published=5 skipped=6" in rep.criteria[1].evidence
    assert "judge vetoes=2" in rep.criteria[1].evidence


def test_streak_broken_by_failed_day() -> None:
    rep = evaluate_exit_gate(scan_runs=_runs(10, failed_on={4}), decisions=[], setups=[])
    assert rep.criteria[0].passed is False
    assert "streak: 5 days" in rep.criteria[0].evidence


def test_streak_needs_full_cadence() -> None:
    rep = evaluate_exit_gate(scan_runs=_runs(8, per_day=12), decisions=[], setups=[])
    assert rep.criteria[0].passed is False


def test_resolver_evaluation_does_not_count_as_forecaster_update() -> None:
    p = _decision(SignalDirection.LONG)
    rep = evaluate_exit_gate(
        scan_runs=[], decisions=[p], setups=[_setup(p.id, {"source": "resolver", "outcome": "WIN"})]
    )
    assert rep.criteria[2].passed is False


def test_gate_criterion_lists_rules() -> None:
    rep = evaluate_exit_gate(
        scan_runs=[], decisions=[_decision(None, "RULE_2_MIN_RR"), _decision(None, "RULE_2_MIN_RR"), _decision(None, "RULE_8_MAX_LEVERAGE")], setups=[]
    )
    assert rep.criteria[3].passed and "RULE_2_MIN_RR×2" in rep.criteria[3].evidence
    assert "CloudWatch cross-check unavailable" in rep.criteria[3].evidence


def test_empty_inputs_fail_everything_without_error() -> None:
    rep = evaluate_exit_gate(scan_runs=[], decisions=[], setups=[])
    assert rep.all_passed is False and len(rep.criteria) == 4
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/unit/test_critic_exit_gate.py -q` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/agents/critic/exit_gate.py`**

```python
"""SPEC §5 'End of Slice 2' criteria over journal rows (design §6.4). Pure."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date, timedelta

from src.agents.critic.models import ExitGateReport, GateCriterion
from src.common.models import ScanStatus
from src.persistence.models import DecisionSummary, StoredActiveSetup, StoredScanRun

_GATE_LOG_NOTE = "CloudWatch cross-check unavailable: risk gates do not log rule ids (finding)."


def _longest_streak(healthy: set[date]) -> tuple[int, date | None, date | None]:
    best, best_start, best_end = 0, None, None
    for start in sorted(healthy):
        if start - timedelta(days=1) in healthy:
            continue
        end = start
        while end + timedelta(days=1) in healthy:
            end += timedelta(days=1)
        length = (end - start).days + 1
        if length > best:
            best, best_start, best_end = length, start, end
    return best, best_start, best_end


def _criterion_1(runs: Sequence[StoredScanRun], per_day: int) -> GateCriterion:
    ok: dict[date, int] = defaultdict(int)
    failed: dict[date, int] = defaultdict(int)
    for r in runs:
        day = r.started_at.date()
        if r.status is ScanStatus.SUCCESS:
            ok[day] += 1
        elif r.status is ScanStatus.FAILED:
            failed[day] += 1
    healthy = {d for d, n in ok.items() if n >= per_day and failed.get(d, 0) == 0}
    streak, start, end = _longest_streak(healthy)
    span = f" ({start}..{end})" if start and end else ""
    evidence = f"longest healthy streak: {streak} days{span}; failed runs: {sum(failed.values())}"
    return GateCriterion(number=1, title="7 consecutive days autonomous", passed=streak >= 7, evidence=evidence)


def _criterion_2(decisions: Sequence[DecisionSummary], setup_ids: set) -> GateCriterion:
    published = sum(1 for d in decisions if d.id in setup_ids)
    analyzer_skips = sum(1 for d in decisions if d.direction is None)
    vetoes = sum(1 for d in decisions if d.direction is not None and d.id not in setup_ids)
    skipped = analyzer_skips + vetoes
    evidence = f"published={published} skipped={skipped} (analyzer/gate skips={analyzer_skips}, judge vetoes={vetoes})"
    return GateCriterion(number=2, title=">=5 published and >=5 skipped", passed=published >= 5 and skipped >= 5, evidence=evidence)


def _criterion_3(setups: Sequence[StoredActiveSetup]) -> GateCriterion:
    n = sum(
        1
        for s in setups
        if s.last_evaluated_at is not None
        and s.latest_evaluation is not None
        and "status" in s.latest_evaluation
        and s.latest_evaluation.get("source") != "resolver"
    )
    return GateCriterion(number=3, title="Forecaster updated >=1 active setup", passed=n >= 1, evidence=f"forecaster-evaluated setups={n}")


def _criterion_4(decisions: Sequence[DecisionSummary]) -> GateCriterion:
    rules = Counter(d.violated_rule for d in decisions if d.violated_rule)
    listing = ", ".join(f"{r}×{n}" for r, n in rules.most_common()) or "none"
    evidence = f"gate rejections={sum(rules.values())}; rules: {listing}. {_GATE_LOG_NOTE}"
    return GateCriterion(number=4, title="Risk gates prevented >=1 rule-violating signal", passed=sum(rules.values()) >= 1, evidence=evidence)


def evaluate_exit_gate(
    *,
    scan_runs: Sequence[StoredScanRun],
    decisions: Sequence[DecisionSummary],
    setups: Sequence[StoredActiveSetup],
    expected_scans_per_day: int = 16,
) -> ExitGateReport:
    setup_ids = {s.signal_id for s in setups}
    criteria = [
        _criterion_1(scan_runs, expected_scans_per_day),
        _criterion_2(decisions, setup_ids),
        _criterion_3(setups),
        _criterion_4(decisions),
    ]
    return ExitGateReport(criteria=criteria, all_passed=all(c.passed for c in criteria))
```

(`set` annotation for `setup_ids` → use `set[UUID]` and import `UUID`; ruff format will wrap long lines.)

- [ ] **Step 4: Run tests and checkpoints** — full command set; expected green.

- [ ] **Step 5: Commit**

```bash
git add src/agents/critic/exit_gate.py tests/unit/test_critic_exit_gate.py
git commit -m "feat(critic): Slice 2 exit-gate evaluation over journal rows

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: DecisionReviewer (Claude, per decision)

**Files:**
- Create: `src/agents/critic/prompts.py`
- Create: `src/agents/critic/reviewer.py`
- Test: `tests/unit/test_critic_reviewer.py`

**Interfaces:**
- Consumes: `structured_completion`, `DEFAULT_MODEL` (`src/common/llm`), `SignalStore` methods `list_pending_reviews`, `count_pending_reviews`, `list_agent_runs_for_scan`, `get_resolution`, `save_review`, `log_agent_run`; `classify` (Task 3); models (Task 1); `AgentRole.CRITIC`.
- Produces:

```python
# prompts.py
REVIEW_SYSTEM_PROMPT: Final[str]
def build_review_user_prompt(record: DecisionRecord, resolution: Resolution | None) -> str

# reviewer.py
@dataclass(frozen=True)
class ReviewSummary:
    reviewed: int
    failed: int
    remaining: int
    cost_usd: float          # sum of successful calls this run

class DecisionReviewer:
    def __init__(self, *, store: SignalStore, client: AsyncAnthropic, model: str = DEFAULT_MODEL,
                 concurrency: int = 4, now: Callable[[], datetime] = _utcnow) -> None
    async def run(self, *, chunk_size: int = 25) -> ReviewSummary
    async def review_one(self, signal: StoredSignal) -> StructuredCompletionResult[DecisionReview] | None
```

Behaviour: `no_setup_before = now() - timedelta(days=7)`, `retry_before = now() - timedelta(days=1)`. For each pending signal (under an `asyncio.Semaphore(concurrency)`): `runs`, `record = classify(...)`, `resolution = await store.get_resolution(signal.id)` (None for NO_SETUP), then `structured_completion(output_schema=DecisionReview, system=REVIEW_SYSTEM_PROMPT, user=build_review_user_prompt(record, resolution), model=self._model, client=self._client, tool_name="emit_decision_review", tool_description="Record your graded review of this trading decision.")`. On success: `save_review(signal_id, decision_kind=record.kind.value, review=result.output.model_dump(mode="json"), model=result.model, token_usage={"tokens_in": result.tokens_in, "tokens_out": result.tokens_out}, cost_usd=result.cost_usd, error=None)` **and** `log_agent_run(scan_id=signal.scan_id, agent_role=AgentRole.CRITIC, strategy=signal.strategy, input_hash=sha256(f"critic:{signal.id}").hexdigest(), output=review dict, latency_ms=result.latency_ms, token_usage=..., cost_usd=result.cost_usd)`. On any exception: `save_review(..., review={}, model=self._model, token_usage={}, cost_usd=None, error=f"{type(exc).__name__}: {exc}"[:2000])`, count `failed`. `remaining = count_pending_reviews(...)` after the batch.

`REVIEW_SYSTEM_PROMPT` (write exactly this, then ruff-format):

```text
You are the Critic for a signal-only Smart Money Concepts (SMC) crypto analyzer.
You grade ONE past trading decision with the benefit of hindsight: you are given
the Analyzer's proposal (or skip), the Historian / Skeptic / Judge outputs that
produced the decision, and the RESOLUTION -- what price actually did afterwards,
computed deterministically from 5-minute candles (WIN = target 1 hit before stop,
LOSS = stop hit first, EXPIRED = entry never triggered within 72h or nothing hit
within 7 days, UNRESOLVABLE = data missing).

Judge the REASONING, not the luck. A well-read setup can lose; a sloppy one can win.
- smc_read_valid: was the structure / zone / premium-discount / liquidity read
  defensible on its own terms? Ignore the outcome for this field.
- skeptic_vindicated: did the market bear out the Skeptic's objection? YES only
  when the objection's mechanism visibly played out; NOT_APPLICABLE if no Skeptic ran.
- judge_correct: given the outcome AND the information available at the time,
  was PUBLISH / PUBLISH_WITH_CAVEAT / SKIP the right ruling?
- gate_correct (GATE_REJECTED only): counterfactually, did the hard rule save a loss
  (true) or block a winner (false)? Leave null otherwise.
- failure_category: the single dominant error, or NONE.
- lesson: one concrete sentence a prompt author could act on. Cite the symbol and
  the specific element (e.g. "OB was in premium; discount rule should have fired").
Traditional indicators (RSI, MACD, moving averages) are forbidden in this system;
never recommend them. Never recommend order execution or position management.
```

`build_review_user_prompt` renders sections `## Decision`, `## Analyzer proposal` (or `## Analyzer skip`), `## Historian`, `## Skeptic`, `## Judge`, `## Resolution (ground truth)` using `json.dumps(obj, indent=2, default=str)` for each dict/model (`model_dump(mode="json")` for models); omit absent sections with the literal line `(not run)`. For the resolution include a one-line human summary first: `"Outcome: WIN after 6h12m in trade; realized 3.00R; MAE -0.40R; MFE 3.20R; TP2 not reached"` (durations from `entered_at`→`resolved_at`, or `"never entered"`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_critic_reviewer.py
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from unittest.mock import AsyncMock, MagicMock

from src.agents.critic.models import DecisionKind, Resolution, ResolutionOutcome
from src.agents.critic.prompts import REVIEW_SYSTEM_PROMPT, build_review_user_prompt
from src.agents.critic.reviewer import DecisionReviewer, ReviewSummary
from src.common.models import AgentRole, SignalDirection, SignalStatus
from src.persistence.models import StoredAgentRun, StoredSignal

T0 = datetime(2026, 7, 1, 8, 3, tzinfo=UTC)


def _proposal_payload(scan_id: UUID) -> dict[str, Any]:
    return {
        "proposal_id": str(uuid4()), "scan_id": str(scan_id), "strategy": "smc",
        "created_at": T0.isoformat(), "symbol": "BTCUSDT", "direction": "LONG",
        "entry_price": 100.0, "stop_loss": 98.0, "take_profit_1": 106.0, "take_profit_2": None,
        "risk_reward_ratio": 3.0, "leverage": 3.0, "risk_percent": 1.0, "tags": ["fvg"],
        "confluence_narrative": "discount FVG", "features": {},
    }


def _signal(scan_id: UUID) -> StoredSignal:
    return StoredSignal(
        id=uuid4(), scan_id=scan_id, symbol="BTCUSDT", strategy="smc", direction=SignalDirection.LONG,
        status=SignalStatus.PUBLISHED, created_at=T0, payload=_proposal_payload(scan_id), tags=[], features={},
    )


def _run(scan_id: UUID, role: AgentRole, output: dict[str, Any]) -> StoredAgentRun:
    return StoredAgentRun(id=uuid4(), scan_id=scan_id, agent_role=role, strategy="smc", input_hash="h",
                          output=output, latency_ms=0, token_usage={}, cost_usd=None, created_at=T0)


def _resolution(signal_id: UUID) -> Resolution:
    return Resolution(signal_id=signal_id, decision_kind=DecisionKind.PUBLISHED, outcome=ResolutionOutcome.WIN,
                      decided_at=T0, entered_at=T0, resolved_at=T0.replace(hour=14), realized_r=3.0,
                      max_adverse_excursion_r=-0.4, max_favorable_excursion_r=3.2, tp2_reached=False,
                      candles_used=72, engine_version="replay-v1")


REVIEW_OK = {
    "smc_read_valid": True, "skeptic_vindicated": "NO", "judge_correct": True, "gate_correct": None,
    "failure_category": "NONE", "lesson": "BTCUSDT discount FVG long was textbook; keep.", "confidence": "HIGH",
}


def _response(payload: dict[str, Any]) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name="emit_decision_review", id="toolu_1", input=payload)
    return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=1200, output_tokens=90))


def _client(responses: list[Any]) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    return client


class FakeStore:
    def __init__(self, signals: list[StoredSignal], runs: dict[UUID, list[StoredAgentRun]],
                 resolutions: dict[UUID, Resolution], remaining: int = 0) -> None:
        self._signals, self._runs, self._res, self._remaining = signals, runs, resolutions, remaining
        self.reviews: list[dict[str, Any]] = []
        self.agent_runs: list[dict[str, Any]] = []

    async def list_pending_reviews(self, *, limit: int, no_setup_before: datetime, retry_before: datetime) -> list[StoredSignal]:
        return self._signals[:limit]

    async def count_pending_reviews(self, *, no_setup_before: datetime, retry_before: datetime) -> int:
        return self._remaining

    async def list_agent_runs_for_scan(self, scan_id: UUID) -> list[StoredAgentRun]:
        return self._runs.get(scan_id, [])

    async def get_resolution(self, signal_id: UUID) -> Resolution | None:
        return self._res.get(signal_id)

    async def save_review(self, **kwargs: Any) -> None:
        self.reviews.append(kwargs)

    async def log_agent_run(self, **kwargs: Any) -> UUID:
        self.agent_runs.append(kwargs)
        return uuid4()


async def test_review_saved_and_logged_to_agent_runs() -> None:
    scan = uuid4()
    sig = _signal(scan)
    store = FakeStore([sig], {scan: [_run(scan, AgentRole.JUDGE, {"ruling": "PUBLISH"})]}, {sig.id: _resolution(sig.id)})
    reviewer = DecisionReviewer(store=store, client=_client([_response(REVIEW_OK)]))
    summary = await reviewer.run(chunk_size=10)
    assert summary.reviewed == 1 and summary.failed == 0
    saved = store.reviews[0]
    assert saved["signal_id"] == sig.id and saved["decision_kind"] == "PUBLISHED" and saved["error"] is None
    assert saved["review"]["failure_category"] == "NONE"
    logged = store.agent_runs[0]
    assert logged["agent_role"] is AgentRole.CRITIC and logged["scan_id"] == scan
    assert logged["token_usage"] == {"tokens_in": 1200, "tokens_out": 90}


async def test_failed_call_records_error_row_and_continues() -> None:
    scan1, scan2 = uuid4(), uuid4()
    s1, s2 = _signal(scan1), _signal(scan2)
    store = FakeStore([s1, s2], {}, {s1.id: _resolution(s1.id), s2.id: _resolution(s2.id)}, remaining=3)
    client = _client([RuntimeError("api down"), _response(REVIEW_OK)])
    summary = await DecisionReviewer(store=store, client=client, concurrency=1).run()
    assert summary == ReviewSummary(reviewed=1, failed=1, remaining=3, cost_usd=summary.cost_usd)
    errors = [r for r in store.reviews if r["error"]]
    assert len(errors) == 1 and errors[0]["error"].startswith("RuntimeError")
    assert errors[0]["review"] == {}


def test_user_prompt_includes_resolution_summary_and_sections() -> None:
    from src.agents.critic.inventory import classify
    scan = uuid4()
    sig = _signal(scan)
    record = classify(sig, [_run(scan, AgentRole.SKEPTIC, {"severity": "HIGH", "headline": "DXY up"})])
    text = build_review_user_prompt(record, _resolution(sig.id))
    assert "## Resolution (ground truth)" in text and "Outcome: WIN" in text and "3.00R" in text
    assert "## Skeptic" in text and "DXY up" in text
    assert "## Judge" in text and "(not run)" in text


def test_system_prompt_forbids_indicators() -> None:
    assert "RSI" in REVIEW_SYSTEM_PROMPT and "forbidden" in REVIEW_SYSTEM_PROMPT
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/unit/test_critic_reviewer.py -q` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `prompts.py` and `reviewer.py` per the Interfaces/Behaviour blocks. Use `asyncio.gather(*(self._guarded(sig) for sig in signals))` where `_guarded` acquires the semaphore and returns `("ok", result) | ("failed", None)`; sum `cost_usd` over successes (treat `None` as 0.0).

- [ ] **Step 4: Run tests and checkpoints** — full set; green.

- [ ] **Step 5: Commit**

```bash
git add src/agents/critic/prompts.py src/agents/critic/reviewer.py tests/unit/test_critic_reviewer.py
git commit -m "feat(critic): DecisionReviewer — Claude grades each resolved decision

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Aggregates, CriticReport call and markdown

**Files:**
- Create: `src/agents/critic/report.py`
- Modify: `src/agents/critic/prompts.py` (add `REPORT_SYSTEM_PROMPT`, `build_report_user_prompt`)
- Test: `tests/unit/test_critic_report.py`

**Interfaces:**
- Consumes: `DecisionSummary`, `StoredScanRun`, `StoredActiveSetup`, `StoredCriticReport`; `structured_completion`; `CriticReport`, `ExitGateReport` (Task 1); `evaluate_exit_gate` (Task 10); store methods `list_decision_summaries`, `list_scan_runs_between`, `list_active_setups`, `save_critic_report`, `log_agent_run`.
- Produces:

```python
@dataclass(frozen=True)
class Aggregates:
    period_start: datetime
    period_end: datetime
    total_decisions: int
    by_kind: dict[str, int]                         # PUBLISHED / VETOED / GATE_REJECTED / NO_SETUP / UNRESOLVED_PROPOSAL
    outcomes_by_kind: dict[str, dict[str, int]]     # kind -> {WIN, LOSS, EXPIRED, UNRESOLVABLE}
    win_rate_by_kind: dict[str, float | None]       # WIN / (WIN+LOSS)
    total_r_published: float
    by_symbol: dict[str, dict[str, int]]            # symbol -> outcomes (PUBLISHED only)
    by_hour_utc: dict[int, dict[str, int]]          # decision hour -> outcomes (PUBLISHED only)
    by_rule: dict[str, dict[str, int]]              # violated_rule -> counterfactual outcomes
    skeptic_value: dict[str, float | None]          # {"overruled_win_rate", "respected_win_rate"}  (needs review.skeptic_vindicated / judge data; compute from reviews: overruled == review.skeptic_vindicated in {YES,NO} and kind PUBLISHED)
    judge_value: float | None                       # share of VETOED whose counterfactual was LOSS/EXPIRED (veto saved us)
    gate_value: float | None                        # share of GATE_REJECTED whose counterfactual was LOSS/EXPIRED
    failure_categories: dict[str, int]              # from reviews
    reviews_present: int
    reviews_failed: int

@dataclass(frozen=True)
class Lesson:
    signal_id: UUID     # DecisionSummary.id — the id the report call must cite in ProposedRuleChange.evidence_signal_ids
    symbol: str
    kind: str           # decision_kind_of(row)
    lesson: str         # review["lesson"], stripped

def compute_aggregates(decisions: Sequence[DecisionSummary], *, period_start, period_end) -> Aggregates
def recent_lessons(decisions: Iterable[DecisionSummary], *, limit: int = 25) -> list[Lesson]
#   newest `limit` lessons by (created_at, id) desc; rows without a successful review or a non-empty string `lesson` are skipped
def render_markdown(*, aggregates: Aggregates, report: CriticReport, exit_gate: ExitGateReport | None, lessons: Sequence[Lesson]) -> str
def build_report_user_prompt(aggregates: Aggregates, lessons: Sequence[Lesson]) -> str      # prompts.py; one `signal_id=… symbol=… kind=…: lesson` line per Lesson

async def fetch_all_decisions(store, *, since, until, page=500) -> list[DecisionSummary]      # paginate list_decision_summaries
async def fetch_all_scan_runs(store, *, since, until, page=500) -> list[StoredScanRun]
async def fetch_all_setups(store, *, page=500) -> list[StoredActiveSetup]

class CriticReporter:
    def __init__(self, *, store, client, model=DEFAULT_MODEL, now=_utcnow) -> None
    async def build(self, *, since: datetime, until: datetime, include_exit_gate: bool) -> ReportBuild

@dataclass(frozen=True)
class ReportBuild:          # Task 13 follow-up: the CLI prints decisions=<n> from decision_count
    stored: StoredCriticReport
    exit_gate: ExitGateReport | None
    decision_count: int
```

`CriticReporter.build`: fetch decisions (+ scan runs / setups when `include_exit_gate`), `aggregates = compute_aggregates(...)`, `report = structured_completion(output_schema=CriticReport, system=REPORT_SYSTEM_PROMPT, user=build_report_user_prompt(aggregates, lessons), tool_name="emit_critic_report", ...)`, log to `agent_runs` with `agent_role=AgentRole.CRITIC` (`scan_id` = the most recent decision's `scan_id`, or skip logging when there are zero decisions), `exit_gate = evaluate_exit_gate(...)` when requested, `markdown = render_markdown(...)`, `stored = StoredCriticReport(id=uuid4(), period_start=since, period_end=until, report=report.model_dump(mode="json"), markdown=markdown, pr_url=None, created_at=now())`, `await store.save_critic_report(stored)`; return `ReportBuild(stored, exit_gate, decision_count=len(decisions))`.

`REPORT_SYSTEM_PROMPT` (exact text):

```text
You are the weekly Critic for a signal-only SMC crypto analyzer. You receive
pre-computed statistics over a period (win rates by decision kind, symbol, hour,
rule; Skeptic / Judge / gate value; failure-category counts) plus the per-decision
lessons written by the per-decision reviewer. Produce:
- summary: what worked, what did not, in plain language with the numbers.
- key_patterns: recurring, evidence-backed patterns (max 20).
- skeptic_assessment / judge_assessment / gate_assessment: is each agent adding
  value? Quote the relevant rates.
- proposed_changes: at most 10 concrete, minimal changes. Each `target` must be one
  of SKEPTIC_SYSTEM_PROMPT, JUDGE_SYSTEM_PROMPT, FORECASTER_SYSTEM_PROMPT,
  risk_gates.<CONSTANT>, or "SPEC §1.6 rule N". `change` is the exact wording or
  value to adopt. `rationale` cites the statistic. `evidence_signal_ids` lists the
  signal ids from the lessons that support it.
Never propose RSI/MACD/moving averages/Bollinger (forbidden). Never propose order
execution, position sizing changes above SPEC §1.6 limits, or removing a hard rule.
Prefer "no change" over speculative change when the sample is small (say so).
```

`render_markdown` layout:

```markdown
# Critic report — {period_start:%Y-%m-%d} → {period_end:%Y-%m-%d}

## Slice 2 exit gate            (only when exit_gate given)
| # | Criterion | Result | Evidence |
...
**Verdict:** PASS / FAIL

## Summary
{report.summary}

## Numbers
| Kind | Decisions | WIN | LOSS | EXPIRED | Win rate |
...
Total realised R (published): {total_r_published:+.2f}
Skeptic value: overruled win-rate X% vs respected Y%
Judge value: Z% of vetoes avoided a loss/expiry
Gate value: W% of rejections avoided a loss/expiry
### By symbol (published) / ### By hour UTC (published) / ### By rule (counterfactual) — small tables
### Failure categories — table

## Key patterns
- ...

## Agent assessments
**Skeptic.** ... **Judge.** ... **Gates.** ...

## Proposed changes
- [ ] **{target}** — {change}
  _Why:_ {rationale}  _Evidence:_ `id1`, `id2`

## Recent lessons
- **{symbol}** ({kind}): {lesson} `{signal_id}`

_Generated by Critic v0. Reviews: {reviews_present} ok / {reviews_failed} failed._
```

Lessons are `Lesson` value objects rather than bare `(symbol, kind, lesson)` tuples because `REPORT_SYSTEM_PROMPT` asks the model to fill `ProposedRuleChange.evidence_signal_ids` from the lessons and design §6.3 requires each proposal to name its evidence decision ids: the `signal_id` has to travel with the lesson text into the user prompt, and the markdown echoes it so a reviewer can look the decision up.

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_critic_report.py` covering: `compute_aggregates` on a fixture of 8 `DecisionSummary` rows (3 PUBLISHED: WIN/WIN/LOSS with `realized_r` 3,3,-1 → `win_rate_by_kind["PUBLISHED"] == 2/3`, `total_r_published == 5.0`; 2 VETOED: LOSS, WIN → `judge_value == 0.5`; 2 GATE_REJECTED: LOSS, EXPIRED → `gate_value == 1.0`; 1 NO_SETUP; one review with `failure_category="WRONG_ZONE"` → `failure_categories == {"WRONG_ZONE": 1}`); `render_markdown` includes the gate table when given and omits the `## Slice 2 exit gate` heading when `exit_gate=None`; proposed changes render as checklist items; `fetch_all_decisions` pages until a short page (fake store returning 500 then 3 rows → 2 calls, cursor = last row's `(created_at, id)`); `CriticReporter.build` with the fake client pattern returns a `StoredCriticReport` whose `report["proposed_changes"]` round-trips and calls `save_critic_report` once.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** `report.py` and the prompt additions. Empty period (zero decisions): skip the LLM call, use a fixed `CriticReport(summary="No decisions in period.", key_patterns=[], skeptic_assessment="n/a", judge_assessment="n/a", gate_assessment="n/a", proposed_changes=[])`.

- [ ] **Step 4: Run tests and checkpoints** — green.

- [ ] **Step 5: Commit**

```bash
git add src/agents/critic/report.py src/agents/critic/prompts.py tests/unit/test_critic_report.py
git commit -m "feat(critic): period aggregates, CriticReport call and markdown rendering

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: `scripts/run_critic.py` CLI (Actions side)

**Files:**
- Create: `scripts/run_critic.py`
- Test: `tests/unit/test_run_critic.py`

**Interfaces:**
- Consumes: `DecisionReviewer` (Task 11), `CriticReporter` (Task 12), `create_store`, `get_settings`, `hydrate_secrets_env` (`src/config/secrets`), `TelegramNotifier`, `AsyncAnthropic`.
- Produces a CLI:

```
python -m scripts.run_critic review --chunk-size 25 [--concurrency 4]
    stdout last line:  REVIEW reviewed=<n> failed=<f> remaining=<k> cost_usd=<x.xxxx>
    exit 0 always (failures are rows, not crashes) unless the store/API is unreachable -> exit 2

python -m scripts.run_critic report --since <ISO> --until <ISO> [--exit-gate] --out <path.md> [--notify]
    writes markdown to --out; stdout last line: REPORT id=<uuid> decisions=<n> gate=<PASS|FAIL|n/a>
    --notify sends the Telegram summary: "🧪 Critic report {period}: {summary[:300]} … {n} proposed changes"
    exit 0 on success, 2 on failure

python -m scripts.run_critic pending
    stdout: PENDING reviews=<k>   (used by the workflow to short-circuit)
```

Settings: `hydrate_secrets_env()` then `get_settings()` exactly like `run_scan._load_settings`; the workflow sets `PERSISTENCE_BACKEND=dataapi`, `DB_CLUSTER_ARN`, `DB_SECRET_ARN`, `DB_NAME`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` in the environment (so `hydrate_secrets_env` is a no-op there — no SSM parameter names set). Machine-readable last lines are a contract with `critic.yml`; tests assert them with `capsys`.

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_run_critic.py`: monkeypatch `create_store`, `AsyncAnthropic`, `DecisionReviewer`, `CriticReporter`, `TelegramNotifier` on the module; assert (a) `review` prints `REVIEW reviewed=1 failed=0 remaining=4 cost_usd=0.0123` and returns 0; (b) `report --exit-gate --out <tmp>/r.md` writes the markdown, prints `REPORT id=... gate=PASS`, and with `--notify` awaits `notifier.send` once; (c) `pending` prints `PENDING reviews=7`; (d) a store exception in `review` returns exit code 2 and logs; (e) `--since`/`--until` parse ISO-8601 with `Z` or offset into aware datetimes (test a pure `_parse_iso` helper).

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** with `argparse` sub-commands; `async def _amain(args) -> int`; `def main(argv: list[str] | None = None) -> int`; `if __name__ == "__main__": raise SystemExit(main())`. Close store/client/notifier in `finally` blocks like `run_scan._run_forecaster`.

- [ ] **Step 4: Run tests and checkpoints** — `mypy --strict src/ scripts/` must include the new script; green.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_critic.py tests/unit/test_run_critic.py
git commit -m "feat(critic): run_critic CLI (review | report | pending) for the Actions runner

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 14: EventBridge `rate(2 days)` resolve schedule

**Files:**
- Modify: `infrastructure/stacks/scheduling_stack.py`
- Test: `tests/infra/test_stacks.py` (append)

**Interfaces:**
- Produces: `SchedulingStack.resolver_schedule` — `scheduler.Schedule` with `scheduler.ScheduleExpression.rate(Duration.days(2))`, target `scheduler_targets.LambdaInvoke(scan_function, input=scheduler.ScheduleTargetInput.from_object({"mode": "resolve", "chunk_size": 25}))`, description `"Critic v0 outcome resolver: replays unresolved decisions against 5m candles every 2 days (design 2026-09-22 §7.2)."`, plus `CfnOutput("ResolverScheduleName")`.

- [ ] **Step 1: Write the failing test** (append to `tests/infra/test_stacks.py`, following how it already asserts the `ForecasterSweep` schedule's `Input` — use `Template.from_stack(...)` and `has_resource_properties("AWS::Scheduler::Schedule", {...})`):

```python
def test_scheduling_stack_has_resolver_rate_schedule(scheduling_template) -> None:  # reuse the file's fixture name
    scheduling_template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "rate(2 days)",
            "Target": Match.object_like({"Input": '{"mode":"resolve","chunk_size":25}'}),
        },
    )
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/infra -q -k resolver` (needs `pip install -r infrastructure/requirements.txt`; if the infra tests are skipped locally because the CDK deps are absent, install them — they are required for this task).

- [ ] **Step 3: Implement** in `SchedulingStack.__init__` after the forecaster schedule (import `Duration` from `aws_cdk` if not already imported):

```python
        # Critic v0 Layer 1 (design 2026-09-22 §7.2): every 2 days, resolve
        # unresolved decisions against real price action. Same Lambda, resolve mode.
        self.resolver_schedule = scheduler.Schedule(
            self,
            "OutcomeResolver",
            schedule=scheduler.ScheduleExpression.rate(Duration.days(2)),
            target=scheduler_targets.LambdaInvoke(
                scan_function,
                input=scheduler.ScheduleTargetInput.from_object(
                    {"mode": "resolve", "chunk_size": 25}
                ),
            ),
            description=(
                "Critic v0 outcome resolver: replays unresolved decisions against 5m "
                "candles every 2 days (design 2026-09-22 §7.2)."
            ),
        )
        CfnOutput(self, "ResolverScheduleName", value=self.resolver_schedule.schedule_name)
```

Update the module docstring's schedule list. If `cdk-nag` flags anything, mirror the existing `NagSuppressions` used for the other schedules.

- [ ] **Step 4: Run tests and checkpoints** — `python -m pytest tests/infra -q && python -m ruff check . && python -m ruff format --check .`; green.

- [ ] **Step 5: Commit**

```bash
git add infrastructure/stacks/scheduling_stack.py tests/infra/test_stacks.py
git commit -m "feat(infra): rate(2 days) EventBridge schedule for the outcome resolver

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 15: `critic.yml` workflow, docs and SPEC amendments

**Files:**
- Create: `.github/workflows/critic.yml`
- Create: `docs/critic-reports/.gitkeep`
- Modify: `docs/operations.md` (new §7 "Critic v0" + IAM grants), `docs/PROJECT_STATE.md` (§2 status, §7 open items), `CLAUDE.md` (§2 Critic line, §5 schedule table, §7 status), `SPEC.md` (§4 Steps 2.14 / 3.5–3.7 notes; §5 End of Slice 2 evidence line), `docs/step-2.14-validation-plan.md` (status line → superseded by the automated gate report, link the design)
- Test: `tests/unit/test_workflows.py` (create; PyYAML is available via pre-commit/CDK deps — if not importable in the test env, parse with a minimal regex check instead)

**Workflow contract (design §7.1, all-cloud):**

```yaml
# .github/workflows/critic.yml
name: Critic
on:
  schedule:
    - cron: "30 22 */2 * *"       # every other day, after the 22:03 UTC wrap scan
  workflow_dispatch:
    inputs:
      mode:
        type: choice
        options: [incremental, backfill]
        default: incremental
      confirm_cost:
        type: boolean
        default: false
        description: "backfill only: continue past the first review chunk"
      max_chunks:
        default: "40"
      period_start:
        default: ""
        description: "ISO date; empty = 14 days ago (incremental) or 2026-06-12 (backfill)"
permissions:
  id-token: write
  contents: write
  pull-requests: write
concurrency:
  group: critic
  cancel-in-progress: false
env:
  AWS_REGION: ${{ vars.AWS_REGION }}
  SCAN_FUNCTION: CryptoSignals-Compute-ScanLambdaDD7505A5-rfFwKEhPAnlo
  PERSISTENCE_BACKEND: dataapi
  DB_NAME: signals
jobs:
  critic:
    runs-on: ubuntu-latest
    timeout-minutes: 300
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - run: pip install -e ".[dev]"
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}
      - name: Resolve ARNs + secrets
        id: cfg
        run: |
          set -euo pipefail
          q() { aws cloudformation describe-stacks --stack-name CryptoSignals-Data \
                --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text; }
          echo "DB_CLUSTER_ARN=$(q ClusterArn)" >> "$GITHUB_ENV"
          echo "DB_SECRET_ARN=$(q DbSecretArn)" >> "$GITHUB_ENV"
          ANTH=$(aws ssm get-parameter --with-decryption --name /crypto-signals/anthropic-api-key --query Parameter.Value --output text)
          TG=$(aws ssm get-parameter --with-decryption --name /crypto-signals/telegram-bot-token --query Parameter.Value --output text)
          echo "::add-mask::$ANTH"; echo "ANTHROPIC_API_KEY=$ANTH" >> "$GITHUB_ENV"
          BOT=$(echo "$TG" | python -c 'import json,sys;print(json.load(sys.stdin)["bot_token"])')
          CHAT=$(echo "$TG" | python -c 'import json,sys;print(json.load(sys.stdin)["chat_id"])')
          echo "::add-mask::$BOT"; echo "TELEGRAM_BOT_TOKEN=$BOT" >> "$GITHUB_ENV"; echo "TELEGRAM_CHAT_ID=$CHAT" >> "$GITHUB_ENV"
      - name: Apply schema (idempotent, via Lambda migrate mode)
        run: |
          aws lambda invoke --function-name "$SCAN_FUNCTION" --cli-binary-format raw-in-base64-out \
            --payload '{"mode":"migrate"}' out.json && cat out.json && grep -q '"ok": true' out.json
      - name: Resolve loop (Lambda, Mumbai)
        run: |
          set -euo pipefail
          MAX=${{ inputs.max_chunks || '40' }}
          for i in $(seq 1 "$MAX"); do
            aws lambda invoke --function-name "$SCAN_FUNCTION" --cli-binary-format raw-in-base64-out \
              --payload '{"mode":"resolve","chunk_size":25}' out.json >/dev/null
            cat out.json
            REMAINING=$(python -c 'import json;print(json.load(open("out.json"))["remaining"])')
            RESOLVED=$(python -c 'import json;print(json.load(open("out.json"))["resolved"])')
            if [ "$REMAINING" = "0" ]; then echo "resolve queue drained"; break; fi
            if [ "$RESOLVED" = "0" ]; then echo "::warning::no progress; stopping resolve loop"; break; fi
          done
      - name: Review loop (Claude)
        id: review
        run: |
          set -euo pipefail
          MODE=${{ inputs.mode || 'incremental' }}
          CONFIRM=${{ inputs.confirm_cost || 'false' }}
          MAX=${{ inputs.max_chunks || '40' }}
          for i in $(seq 1 "$MAX"); do
            LINE=$(python -m scripts.run_critic review --chunk-size 25 | tail -n 1); echo "$LINE"
            REMAINING=$(echo "$LINE" | sed -E 's/.*remaining=([0-9]+).*/\1/')
            COST=$(echo "$LINE" | sed -E 's/.*cost_usd=([0-9.]+).*/\1/')
            if [ "$MODE" = "backfill" ] && [ "$CONFIRM" != "true" ]; then
              python - "$COST" "$REMAINING" <<'PY'
          import sys; cost, rem = float(sys.argv[1]), int(sys.argv[2])
          per = cost / 25 if cost else 0.0
          print(f"::notice::first backfill chunk cost ${cost:.4f} (~${per:.4f}/review); ~{rem} remaining -> est ${per*rem:.2f}. Re-dispatch with confirm_cost=true to continue.")
          PY
              echo "stopped_for_cost=true" >> "$GITHUB_OUTPUT"; exit 0
            fi
            if [ "$REMAINING" = "0" ]; then break; fi
          done
      - name: Report + PR + Telegram
        if: steps.review.outputs.stopped_for_cost != 'true'
        env: { GH_TOKEN: "${{ github.token }}" }
        run: |
          set -euo pipefail
          MODE=${{ inputs.mode || 'incremental' }}
          START="${{ inputs.period_start }}"
          if [ -z "$START" ]; then
            if [ "$MODE" = "backfill" ]; then START=2026-06-12T00:00:00Z; else START=$(date -u -d '14 days ago' +%Y-%m-%dT00:00:00Z); fi
          fi
          UNTIL=$(date -u +%Y-%m-%dT%H:%M:%SZ); DAY=$(date -u +%Y-%m-%d)
          GATE=""; [ "$MODE" = "backfill" ] && GATE="--exit-gate"
          OUT="docs/critic-reports/$DAY.md"
          python -m scripts.run_critic report --since "$START" --until "$UNTIL" $GATE --out "$OUT" --notify | tail -n 1
          git config user.name "critic-bot"; git config user.email "critic-bot@users.noreply.github.com"
          BR="claude/proposed-rules-$DAY"
          git checkout -B "$BR"; git add "$OUT"
          git commit -m "critic: report $DAY" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" || { echo "nothing to commit"; exit 0; }
          git push -f origin "$BR"
          gh pr create --base main --head "$BR" --title "Critic report $DAY" --body-file "$OUT" \
            || gh pr edit "$BR" --body-file "$OUT"
      - uses: actions/upload-artifact@v4
        if: always()
        with: { name: critic-report, path: docs/critic-reports/*.md, if-no-files-found: ignore }
```

Notes for the implementer: the `python - ... <<'PY'` heredoc inside `run:` must have its body **un-indented relative to the YAML block scalar's indentation** (YAML strips the common indent); test the parse with `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/critic.yml'))"`. Use the physical Lambda name from `docs/PROJECT_STATE.md §3`; also read it from the `CryptoSignals-Compute` stack output if one exists (`grep -n CfnOutput infrastructure/stacks/compute_stack.py`) and prefer that.

**IAM (operator, outside the repo — document, do not automate):** the OIDC deploy role needs, in addition to what it has: `cloudformation:DescribeStacks` on `CryptoSignals-Data`; `lambda:InvokeFunction` on the scan Lambda; `rds-data:ExecuteStatement`, `rds-data:BatchExecuteStatement` on the cluster ARN; `secretsmanager:GetSecretValue` on the DB secret; `ssm:GetParameter` on `/crypto-signals/anthropic-api-key` and `/crypto-signals/telegram-bot-token`; `kms:Decrypt` on the SSM key if a CMK is used. Put the exact JSON policy in `docs/operations.md §7`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_workflows.py
from pathlib import Path

import yaml

WF = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "critic.yml"


def test_critic_workflow_parses_and_has_contract() -> None:
    doc = yaml.safe_load(WF.read_text(encoding="utf-8"))
    on = doc.get("on") or doc.get(True)  # PyYAML parses bare `on:` as True
    assert on["schedule"][0]["cron"] == "30 22 */2 * *"
    assert set(on["workflow_dispatch"]["inputs"]) == {"mode", "confirm_cost", "max_chunks", "period_start"}
    assert doc["permissions"] == {"id-token": "write", "contents": "write", "pull-requests": "write"}
    steps = doc["jobs"]["critic"]["steps"]
    names = [s.get("name", "") for s in steps]
    assert "Resolve loop (Lambda, Mumbai)" in names and "Review loop (Claude)" in names
    assert any('"mode":"resolve"' in (s.get("run") or "") for s in steps)
    assert any("scripts.run_critic review" in (s.get("run") or "") for s in steps)
    assert any("gh pr create" in (s.get("run") or "") for s in steps)
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/unit/test_workflows.py -q` → `FileNotFoundError`.

- [ ] **Step 3: Implement** the workflow file exactly per the contract, then the docs:

- `docs/operations.md` — new `## 7. Critic v0 (decision review)`: what runs where, how to dispatch a backfill (`gh workflow run critic.yml -f mode=backfill`, then again with `-f confirm_cost=true`), where reports land, the IAM policy JSON, the cost guard, troubleshooting (`remaining` never reaching 0 → check `PROVIDER_ERROR` in the Lambda log group).
- `docs/PROJECT_STATE.md` — §2: Step 2.14 status "automated by Critic v0 (this branch); exit gate is the backfill report's first section"; §7: replace the IAM open item with the expanded grant list; add the three findings (Judge veto stored as PUBLISHED; `session` hardcoded AD_HOC / rule 7 dead; gates don't log) as new open items.
- `CLAUDE.md` — §2 Critic bullet: "Critic v0 (Slice 2.14, GitHub Actions, every 2 days): resolves outcomes deterministically then grades decisions; PR on `claude/proposed-rules-YYYY-MM-DD`"; §5 table: add `rate(2 days)` resolver and the Actions cron; §7: Step 2.14 in progress via Critic v0.
- `SPEC.md` — under Step 2.14 add a "Delivered by" note pointing at the design; under Steps 3.5–3.7 add "Pulled forward as Critic v0 (no pgvector; Actions runtime; 2-day cadence; GITHUB_TOKEN PR). Step 3.4 embeddings plug into `report.py` later."; §5 End of Slice 2: "Evidence: `docs/critic-reports/<date>.md` §Slice 2 exit gate."
- `docs/step-2.14-validation-plan.md` — set Status to "superseded: automated by Critic v0 (see design + `critic.yml`); manual SQL kept for reference" and fix the four query pitfalls the review found (16 rows/day; published = has active_setups row; Forecaster writes no agent_runs; no `RULE_` log lines).

- [ ] **Step 4: Run tests and checkpoints** — `python -m pytest -q && python -m ruff check . && python -m ruff format --check . && python -m mypy --strict src/ scripts/ && pre-commit run --all-files`; green.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/critic.yml docs/critic-reports/.gitkeep docs/operations.md docs/PROJECT_STATE.md CLAUDE.md SPEC.md docs/step-2.14-validation-plan.md tests/unit/test_workflows.py
git commit -m "feat(critic): critic.yml every-2-days workflow (resolve loop -> review -> report PR) + docs/SPEC amendments

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review (done while writing)

- **Spec coverage:** §3 → Task 3; §5.1–5.5 → Tasks 2, 4, 8, 9; §6.1–6.3 → Tasks 11, 12; §6.4 → Task 10; §7.1 → Task 15; §7.2 → Task 14; §7.3 cost guard → Task 15 (`confirm_cost`); §8 → Tasks 5–7; §9 layout → file map; §10 SPEC amendments → Task 15; §11 error table → Tasks 8, 11, 15; §12 testing → every task. Alarm history / Cost Explorer sections of the gate are **not** in this plan (kept to DB evidence + `agent_runs.cost_usd`); the spec's §6.4 wording is relaxed accordingly in Task 15's SPEC/design touch-up.
- **Type consistency:** `Resolution`, `ResolveSummary`, `ReviewSummary`, `DecisionSummary`, `StoredCriticReport`, store method names and kwargs are identical across Tasks 5–13. `now` is always a zero-arg callable returning an aware datetime.
- **Placeholders:** none; Task 12/13 tests are specified as explicit assertions rather than full code to keep the plan readable — the implementer writes them before the implementation, as in every other task.
