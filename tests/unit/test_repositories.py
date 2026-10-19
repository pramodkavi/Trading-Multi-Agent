"""Unit tests for src.persistence.repositories with mocked asyncpg.

These tests do not touch a real database. They verify:
- repositories call conn.execute / fetchrow / fetch with the right SQL shape
- parameter binding maps to the expected column order
- JSONB serialization / deserialization round-trips through _to_jsonb /
  _parse_jsonb_field
- discriminating SignalProposal vs SkipDecision drives status / direction
- ScanRunRepository's complete/fail SQL guards on status=RUNNING

The live-DB side is covered by tests/integration/test_repositories_integration.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.agents.critic.models import DecisionKind, Resolution, ResolutionOutcome
from src.common.models import (
    ActiveSetupStatus,
    AgentRole,
    ScanStatus,
    SetupEvaluationStatus,
    SignalDirection,
    SignalOutcome,
    SignalProposal,
    SignalStatus,
    SkipDecision,
    SkipReason,
)
from src.persistence import (
    AgentRunRepository,
    ScanRunRepository,
    SetupEvaluationRepository,
    SignalRepository,
    StoredActiveSetup,
    StoredAgentRun,
    StoredCriticReport,
    StoredScanRun,
    StoredSetupEvaluation,
    StoredSignal,
)
from src.persistence.repositories import (
    DecisionRepository,
    _parse_jsonb_field,
    _record_to_dict,
    _to_jsonb,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_long_proposal(**overrides: object) -> SignalProposal:
    base: dict[str, object] = {
        "scan_id": uuid4(),
        "strategy": "smc",
        "symbol": "BTCUSDT",
        "direction": SignalDirection.LONG,
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit_1": 115.0,
        "risk_reward_ratio": 3.0,
        "leverage": 5.0,
        "risk_percent": 1.0,
        "confluence_narrative": "Bullish OB tap with liquidity sweep below equal lows.",
    }
    base.update(overrides)
    return SignalProposal(**base)  # type: ignore[arg-type]


def _make_skip(**overrides: object) -> SkipDecision:
    base: dict[str, object] = {
        "scan_id": uuid4(),
        "strategy": "smc",
        "symbol": "ETHUSDT",
        "reason": SkipReason.NO_CLEAR_BIAS,
        "details": "Consolidation; bias unclear within freshness window.",
    }
    base.update(overrides)
    return SkipDecision(**base)  # type: ignore[arg-type]


def _make_conn_mock() -> MagicMock:
    """Build a mock asyncpg connection with the four async methods we use."""
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.fetch = AsyncMock()
    conn.fetchval = AsyncMock()
    return conn


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestToJsonb:
    def test_round_trips_with_parse_jsonb(self) -> None:
        payload = {"a": 1, "b": "two", "c": [1, 2, 3]}
        serialised = _to_jsonb(payload)
        assert isinstance(serialised, str)
        assert _parse_jsonb_field(serialised) == payload

    def test_handles_non_json_values_via_default_str(self) -> None:
        # uuid / datetime are common payload types; default=str handles them.
        u = uuid4()
        ts = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
        serialised = _to_jsonb({"id": u, "when": ts})
        parsed = json.loads(serialised)
        assert parsed["id"] == str(u)
        # default=str gives `str(ts)` which uses a space separator, not 'T'.
        # Either form is acceptable JSON; just verify the round-trip captured
        # the timestamp.
        assert parsed["when"] == str(ts)


class TestParseJsonbField:
    def test_accepts_str_input(self) -> None:
        assert _parse_jsonb_field('{"a": 1}') == {"a": 1}

    def test_accepts_dict_input(self) -> None:
        assert _parse_jsonb_field({"a": 1}) == {"a": 1}

    def test_rejects_non_dict_payload(self) -> None:
        with pytest.raises(ValueError, match="dict"):
            _parse_jsonb_field("[1, 2, 3]")


class TestRecordToDict:
    def test_converts_mapping_like_record(self) -> None:
        # MagicMock with mapping protocol — we just need dict() to work.
        fake = {"id": "x", "name": "y"}
        assert _record_to_dict(fake) == fake  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ScanRunRepository
# ---------------------------------------------------------------------------


class TestScanRunRepository:
    async def test_start_scan_inserts_running_row(self) -> None:
        conn = _make_conn_mock()
        repo = ScanRunRepository(conn)
        scan_id = uuid4()
        started_at = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
        await repo.start_scan(
            scan_id=scan_id,
            started_at=started_at,
            session="LONDON",
            strategy="smc",
            symbols=["BTCUSDT", "ETHUSDT"],
        )
        conn.execute.assert_awaited_once()
        sql, *args = conn.execute.await_args.args
        assert "INSERT INTO scan_runs" in sql
        assert args == [
            scan_id,
            started_at,
            ScanStatus.RUNNING.value,
            "LONDON",
            "smc",
            ["BTCUSDT", "ETHUSDT"],
        ]

    async def test_complete_scan_updates_to_success_and_guards_running(self) -> None:
        conn = _make_conn_mock()
        repo = ScanRunRepository(conn)
        scan_id = uuid4()
        completed_at = datetime(2026, 5, 28, 12, 5, 0, tzinfo=UTC)
        await repo.complete_scan(scan_id=scan_id, completed_at=completed_at)
        sql, *args = conn.execute.await_args.args
        assert "UPDATE scan_runs" in sql
        assert "status = $4" in sql  # guard on previous status (RUNNING)
        assert args == [
            ScanStatus.SUCCESS.value,
            completed_at,
            scan_id,
            ScanStatus.RUNNING.value,
        ]

    async def test_fail_scan_records_error_message(self) -> None:
        conn = _make_conn_mock()
        repo = ScanRunRepository(conn)
        scan_id = uuid4()
        completed_at = datetime(2026, 5, 28, 12, 6, 0, tzinfo=UTC)
        await repo.fail_scan(
            scan_id=scan_id,
            completed_at=completed_at,
            error_message="Binance timeout",
        )
        sql, *args = conn.execute.await_args.args
        assert "UPDATE scan_runs" in sql
        assert args == [
            ScanStatus.FAILED.value,
            completed_at,
            "Binance timeout",
            scan_id,
            ScanStatus.RUNNING.value,
        ]

    async def test_get_by_id_returns_stored_when_present(self) -> None:
        conn = _make_conn_mock()
        repo = ScanRunRepository(conn)
        scan_id = uuid4()
        started_at = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
        conn.fetchrow.return_value = {
            "id": scan_id,
            "started_at": started_at,
            "completed_at": None,
            "status": ScanStatus.RUNNING.value,
            "error_message": None,
            "session": "LONDON",
            "strategy": "smc",
            "symbols": ["BTCUSDT"],
        }
        result = await repo.get_by_id(scan_id)
        assert isinstance(result, StoredScanRun)
        assert result.id == scan_id
        assert result.status is ScanStatus.RUNNING

    async def test_get_by_id_returns_none_when_absent(self) -> None:
        conn = _make_conn_mock()
        repo = ScanRunRepository(conn)
        conn.fetchrow.return_value = None
        result = await repo.get_by_id(uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# SignalRepository
# ---------------------------------------------------------------------------


class TestSignalRepositoryCreate:
    async def test_create_published_for_signal_proposal(self) -> None:
        conn = _make_conn_mock()
        repo = SignalRepository(conn)
        proposal = _make_long_proposal()

        signal_id = await repo.create_signal(proposal)

        assert isinstance(signal_id, UUID)
        conn.execute.assert_awaited_once()
        sql, *args = conn.execute.await_args.args
        assert "INSERT INTO signals" in sql
        assert args[0] == signal_id
        assert args[1] == proposal.scan_id
        assert args[2] == proposal.symbol
        assert args[3] == proposal.strategy
        assert args[4] == SignalDirection.LONG.value
        assert args[5] == SignalStatus.PUBLISHED.value
        # Last positional is the JSONB string.
        payload_json = json.loads(args[6])
        assert payload_json["direction"] == "LONG"
        assert payload_json["scan_id"] == str(proposal.scan_id)

    async def test_create_binds_tags_and_features(self) -> None:
        conn = _make_conn_mock()
        repo = SignalRepository(conn)
        proposal = _make_long_proposal(
            tags=["bullish-ob", "liquidity-sweep"],
            features={"confluence_score": 3, "atr": 12.5},
        )
        await repo.create_signal(proposal)
        _sql, *args = conn.execute.await_args.args
        # tags goes as a native list (asyncpg binds text[]); features as JSONB string.
        assert args[7] == ["bullish-ob", "liquidity-sweep"]
        assert json.loads(args[8]) == {"confluence_score": 3, "atr": 12.5}

    async def test_create_skipped_for_skip_decision(self) -> None:
        conn = _make_conn_mock()
        repo = SignalRepository(conn)
        skip = _make_skip()

        signal_id = await repo.create_signal(skip)

        assert isinstance(signal_id, UUID)
        _sql, *args = conn.execute.await_args.args
        assert args[2] == skip.symbol
        assert args[4] is None  # direction NULL for SKIPPED rows
        assert args[5] == SignalStatus.SKIPPED.value
        payload_json = json.loads(args[6])
        assert payload_json["reason"] == SkipReason.NO_CLEAR_BIAS.value


class TestSignalRepositoryRead:
    def _stored_row(
        self,
        *,
        signal_id: UUID,
        proposal: SignalProposal,
    ) -> dict[str, Any]:
        return {
            "id": signal_id,
            "scan_id": proposal.scan_id,
            "symbol": proposal.symbol,
            "strategy": proposal.strategy,
            "direction": SignalDirection.LONG.value,
            "status": SignalStatus.PUBLISHED.value,
            "created_at": datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
            "payload": json.dumps(proposal.model_dump(mode="json"), default=str),
            # asyncpg returns text[] as a native list and JSONB as a str (no codec).
            "tags": list(proposal.tags),
            "features": json.dumps(dict(proposal.features), default=str),
            "outcome": None,
            "outcome_metadata": None,
        }

    async def test_get_by_id_present(self) -> None:
        conn = _make_conn_mock()
        repo = SignalRepository(conn)
        proposal = _make_long_proposal()
        signal_id = uuid4()
        conn.fetchrow.return_value = self._stored_row(
            signal_id=signal_id,
            proposal=proposal,
        )
        result = await repo.get_by_id(signal_id)
        assert isinstance(result, StoredSignal)
        assert result.id == signal_id
        assert result.status is SignalStatus.PUBLISHED
        # Round-trip through as_proposal to verify JSONB parse path.
        round_tripped = result.as_proposal()
        assert round_tripped.symbol == proposal.symbol
        assert round_tripped.entry_price == proposal.entry_price

    async def test_read_maps_tags_features_outcome(self) -> None:
        conn = _make_conn_mock()
        repo = SignalRepository(conn)
        proposal = _make_long_proposal(tags=["bullish-ob"], features={"confluence_score": 3})
        signal_id = uuid4()
        conn.fetchrow.return_value = self._stored_row(signal_id=signal_id, proposal=proposal)
        result = await repo.get_by_id(signal_id)
        assert isinstance(result, StoredSignal)
        assert result.tags == ["bullish-ob"]
        assert result.features == {"confluence_score": 3}
        assert result.outcome is None  # not set until the Forecaster closes the setup
        assert result.outcome_metadata is None

    async def test_get_by_id_absent(self) -> None:
        conn = _make_conn_mock()
        repo = SignalRepository(conn)
        conn.fetchrow.return_value = None
        assert await repo.get_by_id(uuid4()) is None

    async def test_list_recent_no_symbol_filter(self) -> None:
        conn = _make_conn_mock()
        repo = SignalRepository(conn)
        proposal = _make_long_proposal()
        conn.fetch.return_value = [
            self._stored_row(signal_id=uuid4(), proposal=proposal),
            self._stored_row(signal_id=uuid4(), proposal=proposal),
        ]
        rows = await repo.list_recent(limit=25)
        assert len(rows) == 2
        sql, *args = conn.fetch.await_args.args
        assert "ORDER BY created_at DESC" in sql
        assert "WHERE symbol" not in sql
        assert args == [25]

    async def test_list_recent_with_symbol_filter(self) -> None:
        conn = _make_conn_mock()
        repo = SignalRepository(conn)
        proposal = _make_long_proposal()
        conn.fetch.return_value = [self._stored_row(signal_id=uuid4(), proposal=proposal)]
        rows = await repo.list_recent(symbol="BTCUSDT", limit=10)
        assert len(rows) == 1
        sql, *args = conn.fetch.await_args.args
        assert "WHERE symbol = $1" in sql
        assert args == ["BTCUSDT", 10]

    async def test_list_recent_caps_at_1000(self) -> None:
        conn = _make_conn_mock()
        repo = SignalRepository(conn)
        conn.fetch.return_value = []
        await repo.list_recent(limit=10_000)
        _, *args = conn.fetch.await_args.args
        assert args[0] == 1000  # capped

    async def test_list_recent_floors_at_1(self) -> None:
        conn = _make_conn_mock()
        repo = SignalRepository(conn)
        conn.fetch.return_value = []
        await repo.list_recent(limit=-5)
        _, *args = conn.fetch.await_args.args
        assert args[0] == 1


class TestStoredSignalHelpers:
    def test_as_proposal_round_trips(self) -> None:
        proposal = _make_long_proposal()
        stored = StoredSignal(
            id=uuid4(),
            scan_id=proposal.scan_id,
            symbol=proposal.symbol,
            strategy=proposal.strategy,
            direction=SignalDirection.LONG,
            status=SignalStatus.PUBLISHED,
            created_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
            payload=proposal.model_dump(mode="json"),
        )
        round_tripped = stored.as_proposal()
        assert round_tripped.entry_price == proposal.entry_price

    def test_as_proposal_rejects_skipped_row(self) -> None:
        stored = StoredSignal(
            id=uuid4(),
            scan_id=uuid4(),
            symbol="BTCUSDT",
            strategy="smc",
            direction=None,
            status=SignalStatus.SKIPPED,
            created_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
            payload={},
        )
        with pytest.raises(ValueError, match="PUBLISHED"):
            stored.as_proposal()

    def test_as_skip_round_trips(self) -> None:
        skip = _make_skip()
        stored = StoredSignal(
            id=uuid4(),
            scan_id=skip.scan_id,
            symbol=skip.symbol,
            strategy=skip.strategy,
            direction=None,
            status=SignalStatus.SKIPPED,
            created_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
            payload=skip.model_dump(mode="json"),
        )
        round_tripped = stored.as_skip()
        assert round_tripped.reason is SkipReason.NO_CLEAR_BIAS

    def test_as_skip_rejects_published_row(self) -> None:
        proposal = _make_long_proposal()
        stored = StoredSignal(
            id=uuid4(),
            scan_id=proposal.scan_id,
            symbol=proposal.symbol,
            strategy=proposal.strategy,
            direction=SignalDirection.LONG,
            status=SignalStatus.PUBLISHED,
            created_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
            payload=proposal.model_dump(mode="json"),
        )
        with pytest.raises(ValueError, match="SKIPPED"):
            stored.as_skip()


# ---------------------------------------------------------------------------
# AgentRunRepository
# ---------------------------------------------------------------------------


class TestAgentRunRepository:
    async def test_log_run_serialises_json_fields(self) -> None:
        conn = _make_conn_mock()
        repo = AgentRunRepository(conn)
        scan_id = uuid4()
        run_id = await repo.log_run(
            scan_id=scan_id,
            agent_role=AgentRole.ANALYZER,
            strategy="smc",
            input_hash="abc123",
            output={"decision": "publish", "n": 3},
            latency_ms=42,
            token_usage={"input_tokens": 100, "output_tokens": 50},
            cost_usd=0.0105,
        )
        assert isinstance(run_id, UUID)
        sql, *args = conn.execute.await_args.args
        assert "INSERT INTO agent_runs" in sql
        assert args[0] == run_id
        assert args[1] == scan_id
        assert args[2] == AgentRole.ANALYZER.value
        assert args[3] == "smc"
        assert args[4] == "abc123"
        assert json.loads(args[5]) == {"decision": "publish", "n": 3}
        assert args[6] == 42
        assert json.loads(args[7]) == {"input_tokens": 100, "output_tokens": 50}
        assert args[8] == 0.0105

    async def test_log_run_defaults_token_usage_to_empty_dict(self) -> None:
        conn = _make_conn_mock()
        repo = AgentRunRepository(conn)
        await repo.log_run(
            scan_id=uuid4(),
            agent_role=AgentRole.ANALYZER,
            strategy="smc",
            input_hash="abc",
            output={"result": "skip"},
            latency_ms=10,
        )
        _, *args = conn.execute.await_args.args
        assert json.loads(args[7]) == {}
        assert args[8] is None  # cost_usd None

    async def test_get_by_id_present(self) -> None:
        conn = _make_conn_mock()
        repo = AgentRunRepository(conn)
        run_id = uuid4()
        scan_id = uuid4()
        conn.fetchrow.return_value = {
            "id": run_id,
            "scan_id": scan_id,
            "agent_role": AgentRole.ANALYZER.value,
            "strategy": "smc",
            "input_hash": "abc",
            "output": json.dumps({"decision": "publish"}),
            "latency_ms": 30,
            "token_usage": json.dumps({}),
            "cost_usd": 0.005,
            "created_at": datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
        }
        result = await repo.get_by_id(run_id)
        assert isinstance(result, StoredAgentRun)
        assert result.id == run_id
        assert result.agent_role is AgentRole.ANALYZER
        assert result.output == {"decision": "publish"}

    async def test_get_by_id_absent(self) -> None:
        conn = _make_conn_mock()
        repo = AgentRunRepository(conn)
        conn.fetchrow.return_value = None
        assert await repo.get_by_id(uuid4()) is None


# ---------------------------------------------------------------------------
# SetupEvaluationRepository (Slice 4 Step 4.0, design §5.2)
# ---------------------------------------------------------------------------


class TestSetupEvaluationRepository:
    async def test_setup_evaluation_record_executes_insert(self) -> None:
        conn = _make_conn_mock()
        repo = SetupEvaluationRepository(conn)
        setup_id, signal_id = uuid4(), uuid4()
        eval_id = await repo.record(
            setup_id=setup_id,
            signal_id=signal_id,
            status=SetupEvaluationStatus.AT_RISK,
            reasoning="drifting",
            latency_ms=800,
            token_usage={"tokens_in": 10},
            cost_usd=0.001,
        )
        assert isinstance(eval_id, UUID)
        sql, *args = conn.execute.await_args.args
        assert "INSERT INTO setup_evaluations" in sql
        assert "COALESCE($4, NOW())" in sql
        assert args[0] == eval_id
        assert args[1] == setup_id
        assert args[2] == signal_id
        assert args[3] is None  # evaluated_at -> NOW() DB-side
        assert args[4] == "AT_RISK"
        assert args[5] is None  # outcome
        assert args[6] == "drifting"
        assert args[7] is None  # error
        assert args[8] == 800
        assert json.loads(args[9]) == {"tokens_in": 10}
        assert args[10] == 0.001

    async def test_setup_evaluation_record_error_row_defaults(self) -> None:
        conn = _make_conn_mock()
        ts = datetime(2026, 10, 19, 8, 5, tzinfo=UTC)
        await SetupEvaluationRepository(conn).record(
            setup_id=uuid4(),
            signal_id=uuid4(),
            status=SetupEvaluationStatus.ERROR,
            error="RuntimeError: boom",
            evaluated_at=ts,
        )
        _, *args = conn.execute.await_args.args
        assert args[3] == ts
        assert args[4] == "ERROR"
        assert args[6] is None  # reasoning
        assert args[7] == "RuntimeError: boom"
        assert args[8] == 0
        assert json.loads(args[9]) == {}
        assert args[10] is None  # cost_usd

    async def test_setup_evaluation_record_binds_outcome_value(self) -> None:
        conn = _make_conn_mock()
        await SetupEvaluationRepository(conn).record(
            setup_id=uuid4(),
            signal_id=uuid4(),
            status=SetupEvaluationStatus.INVALIDATED,
            outcome=SignalOutcome.WIN,
        )
        _, *args = conn.execute.await_args.args
        assert args[5] == "WIN"

    async def test_setup_evaluation_list_for_setup_maps_rows(self) -> None:
        conn = _make_conn_mock()
        eval_id, setup_id, sid = uuid4(), uuid4(), uuid4()
        ts = datetime(2026, 10, 19, 8, 5, tzinfo=UTC)
        conn.fetch.return_value = [
            {
                "id": eval_id,
                "setup_id": setup_id,
                "signal_id": sid,
                "evaluated_at": ts,
                "status": "INVALIDATED",
                "outcome": "WIN",
                "reasoning": "target hit",
                "error": None,
                "latency_ms": 900,
                "token_usage": json.dumps({"tokens_in": 5}),
                "cost_usd": 0.002,  # asyncpg hands NUMERIC back as Decimal in prod
            }
        ]
        rows = await SetupEvaluationRepository(conn).list_for_setup(setup_id, limit=999)
        assert len(rows) == 1
        assert isinstance(rows[0], StoredSetupEvaluation)
        assert rows[0].id == eval_id
        assert rows[0].status is SetupEvaluationStatus.INVALIDATED
        assert rows[0].outcome is SignalOutcome.WIN
        assert rows[0].token_usage == {"tokens_in": 5}
        assert rows[0].cost_usd == 0.002
        sql, *args = conn.fetch.await_args.args
        assert "FROM setup_evaluations" in sql
        assert "WHERE setup_id = $1" in sql
        assert "ORDER BY evaluated_at DESC" in sql
        assert args == [setup_id, 200]  # limit clamped to 200

    async def test_setup_evaluation_list_for_setup_floors_limit_at_one(self) -> None:
        conn = _make_conn_mock()
        conn.fetch.return_value = []
        assert await SetupEvaluationRepository(conn).list_for_setup(uuid4(), limit=0) == []
        _, *args = conn.fetch.await_args.args
        assert args[1] == 1


# ---------------------------------------------------------------------------
# DecisionRepository (Critic v0, Step 2.14)
# ---------------------------------------------------------------------------

T0 = datetime(2026, 7, 1, tzinfo=UTC)


def _resolution(**overrides: object) -> Resolution:
    base: dict[str, object] = {
        "signal_id": uuid4(),
        "decision_kind": DecisionKind.VETOED,
        "outcome": ResolutionOutcome.WIN,
        "decided_at": T0,
        "entered_at": None,
        "resolved_at": datetime(2026, 7, 2, tzinfo=UTC),
        "realized_r": 3.0,
        "max_adverse_excursion_r": -0.2,
        "max_favorable_excursion_r": 3.1,
        "tp2_reached": True,
        "candles_used": 10,
        "engine_version": "replay-v1",
    }
    base.update(overrides)
    return Resolution(**base)  # type: ignore[arg-type]


def _signal_row(signal_id: UUID, proposal: SignalProposal) -> dict[str, Any]:
    return {
        "id": signal_id,
        "scan_id": proposal.scan_id,
        "symbol": proposal.symbol,
        "strategy": proposal.strategy,
        "direction": SignalDirection.LONG.value,
        "status": SignalStatus.PUBLISHED.value,
        "created_at": T0,
        "payload": json.dumps(proposal.model_dump(mode="json"), default=str),
        "tags": list(proposal.tags),
        "features": json.dumps(dict(proposal.features), default=str),
        "outcome": None,
        "outcome_metadata": None,
    }


class TestDecisionRepositoryResolverQueue:
    async def test_list_unresolved_binds_cutoff_and_limit(self) -> None:
        conn = _make_conn_mock()
        conn.fetch.return_value = []
        repo = DecisionRepository(conn)
        cutoff = datetime(2026, 7, 1, tzinfo=UTC)
        await repo.list_unresolved_decisions(limit=25, decided_before=cutoff)
        sql, *args = conn.fetch.await_args.args
        assert "LEFT JOIN decision_resolutions" in sql
        assert "r.signal_id IS NULL" in sql
        assert "payload->>'violated_rule' IS NOT NULL" in sql
        assert "ORDER BY s.created_at ASC, s.id ASC" in sql
        assert args == [cutoff, 25]

    async def test_list_unresolved_maps_rows_to_stored_signals(self) -> None:
        conn = _make_conn_mock()
        proposal = _make_long_proposal(tags=["fvg"], features={"confluence_score": 2})
        sid = uuid4()
        conn.fetch.return_value = [_signal_row(sid, proposal)]
        rows = await DecisionRepository(conn).list_unresolved_decisions(limit=10, decided_before=T0)
        assert len(rows) == 1
        assert isinstance(rows[0], StoredSignal)
        assert rows[0].id == sid
        assert rows[0].tags == ["fvg"]
        assert rows[0].features == {"confluence_score": 2}
        assert rows[0].as_proposal().entry_price == proposal.entry_price

    async def test_count_unresolved_uses_fetchval_with_same_predicate(self) -> None:
        conn = _make_conn_mock()
        conn.fetchval.return_value = 7
        n = await DecisionRepository(conn).count_unresolved_decisions(decided_before=T0)
        assert n == 7
        sql, *args = conn.fetchval.await_args.args
        assert "count(*)" in sql
        assert "LEFT JOIN decision_resolutions" in sql
        assert "payload->>'violated_rule' IS NOT NULL" in sql
        assert args == [T0]

    async def test_count_unresolved_treats_null_as_zero(self) -> None:
        conn = _make_conn_mock()
        conn.fetchval.return_value = None
        assert await DecisionRepository(conn).count_unresolved_decisions(decided_before=T0) == 0

    async def test_list_agent_runs_for_scan_maps_rows(self) -> None:
        conn = _make_conn_mock()
        scan_id = uuid4()
        run_id = uuid4()
        conn.fetch.return_value = [
            {
                "id": run_id,
                "scan_id": scan_id,
                "agent_role": AgentRole.JUDGE.value,
                "strategy": "smc",
                "input_hash": "abc",
                "output": json.dumps({"ruling": "SKIP"}),
                "latency_ms": 30,
                "token_usage": json.dumps({"input_tokens": 1}),
                "cost_usd": 0.005,
                "created_at": T0,
            }
        ]
        runs = await DecisionRepository(conn).list_agent_runs_for_scan(scan_id)
        assert len(runs) == 1
        assert isinstance(runs[0], StoredAgentRun)
        assert runs[0].agent_role is AgentRole.JUDGE
        assert runs[0].output == {"ruling": "SKIP"}
        assert runs[0].token_usage == {"input_tokens": 1}
        sql, *args = conn.fetch.await_args.args
        assert "FROM agent_runs" in sql
        assert "WHERE scan_id = $1" in sql
        assert "ORDER BY created_at ASC" in sql
        assert args == [scan_id]

    async def test_save_resolution_upserts_all_columns(self) -> None:
        conn = _make_conn_mock()
        res = _resolution()
        await DecisionRepository(conn).save_resolution(res)
        sql, *args = conn.execute.await_args.args
        assert "INSERT INTO decision_resolutions" in sql
        assert "ON CONFLICT (signal_id) DO UPDATE" in sql
        assert "engine_version = EXCLUDED.engine_version" in sql
        assert args == [
            res.signal_id,
            "VETOED",
            "WIN",
            res.decided_at,
            None,
            res.resolved_at,
            3.0,
            -0.2,
            3.1,
            True,
            10,
            "replay-v1",
        ]
        assert args[11] == "replay-v1"

    async def test_get_resolution_maps_row(self) -> None:
        conn = _make_conn_mock()
        res = _resolution(entered_at=datetime(2026, 7, 1, 1, tzinfo=UTC))
        conn.fetchrow.return_value = {
            "signal_id": res.signal_id,
            "decision_kind": "VETOED",
            "outcome": "WIN",
            "decided_at": res.decided_at,
            "entered_at": res.entered_at,
            "resolved_at": res.resolved_at,
            "realized_r": 3.0,
            "max_adverse_excursion_r": -0.2,
            "max_favorable_excursion_r": 3.1,
            "tp2_reached": True,
            "candles_used": 10,
            "engine_version": "replay-v1",
        }
        got = await DecisionRepository(conn).get_resolution(res.signal_id)
        assert got == res
        sql, *args = conn.fetchrow.await_args.args
        assert "FROM decision_resolutions" in sql
        assert "WHERE signal_id = $1" in sql
        assert args == [res.signal_id]

    async def test_get_resolution_returns_none_when_absent(self) -> None:
        conn = _make_conn_mock()
        conn.fetchrow.return_value = None
        assert await DecisionRepository(conn).get_resolution(uuid4()) is None

    async def test_close_active_setup_for_signal_only_open_rows(self) -> None:
        conn = _make_conn_mock()
        sid = uuid4()
        await DecisionRepository(conn).close_active_setup_for_signal(
            signal_id=sid, status=ActiveSetupStatus.WIN, evaluation={"source": "resolver"}
        )
        sql, *args = conn.execute.await_args.args
        assert "UPDATE active_setups" in sql
        assert "status = 'OPEN'" in sql
        assert "signal_id = $1" in sql
        assert "last_evaluated_at = NOW()" in sql
        assert args[0] == sid
        assert args[1] == "WIN"
        assert json.loads(args[2]) == {"source": "resolver"}


class TestDecisionRepositoryReviewQueue:
    async def test_list_pending_reviews_unions_no_setup_rows(self) -> None:
        conn = _make_conn_mock()
        conn.fetch.return_value = []
        t = datetime(2026, 7, 1, tzinfo=UTC)
        retry = datetime(2026, 7, 2, tzinfo=UTC)
        await DecisionRepository(conn).list_pending_reviews(
            limit=5, no_setup_before=t, retry_before=retry
        )
        sql, *args = conn.fetch.await_args.args
        assert "UNION ALL" in sql
        assert "attempts < 2" in sql
        assert "JOIN decision_resolutions r ON r.signal_id = s.id" in sql
        assert "s.direction IS NULL AND s.payload->>'violated_rule' IS NULL" in sql
        assert "ORDER BY q.created_at ASC, q.id ASC" in sql
        assert args == [t, retry, 5]

    async def test_list_pending_reviews_maps_rows(self) -> None:
        conn = _make_conn_mock()
        proposal = _make_long_proposal()
        sid = uuid4()
        conn.fetch.return_value = [_signal_row(sid, proposal)]
        rows = await DecisionRepository(conn).list_pending_reviews(
            limit=5, no_setup_before=T0, retry_before=T0
        )
        assert [r.id for r in rows] == [sid]
        assert isinstance(rows[0], StoredSignal)

    async def test_count_pending_reviews_uses_fetchval(self) -> None:
        conn = _make_conn_mock()
        conn.fetchval.return_value = 3
        n = await DecisionRepository(conn).count_pending_reviews(
            no_setup_before=T0, retry_before=T0
        )
        assert n == 3
        sql, *args = conn.fetchval.await_args.args
        assert "count(*)" in sql
        assert "UNION ALL" in sql
        assert args == [T0, T0]

    async def test_save_review_increments_attempts_on_conflict(self) -> None:
        conn = _make_conn_mock()
        sid = uuid4()
        await DecisionRepository(conn).save_review(
            signal_id=sid,
            decision_kind="PUBLISHED",
            review={"lesson": "x"},
            model="m",
            token_usage={"in": 1},
            cost_usd=0.01,
            error=None,
        )
        sql, *args = conn.execute.await_args.args
        assert "INSERT INTO decision_reviews" in sql
        assert "attempts = decision_reviews.attempts + 1" in sql
        assert "created_at = NOW()" in sql
        assert args[0] == sid
        assert args[1] == "PUBLISHED"
        assert json.loads(args[2]) == {"lesson": "x"}
        assert args[3] == "m"
        assert json.loads(args[4]) == {"in": 1}
        assert args[5] == 0.01
        assert args[6] is None

    async def test_save_review_error_row(self) -> None:
        conn = _make_conn_mock()
        await DecisionRepository(conn).save_review(
            signal_id=uuid4(),
            decision_kind="NO_SETUP",
            review={},
            model="m",
            token_usage={},
            cost_usd=None,
            error="boom",
        )
        _sql, *args = conn.execute.await_args.args
        assert json.loads(args[2]) == {}
        assert args[5] is None
        assert args[6] == "boom"


class TestDecisionRepositoryReportReads:
    def _summary_row(self, sid: UUID, scan: UUID) -> dict[str, Any]:
        return {
            "id": sid,
            "scan_id": scan,
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "status": "PUBLISHED",
            "created_at": datetime(2026, 7, 1, tzinfo=UTC),
            "violated_rule": None,
            "outcome": "WIN",
            "decision_kind": "PUBLISHED",
            "resolution_outcome": "WIN",
            "realized_r": 3.0,
            "entered_at": datetime(2026, 7, 1, 1, tzinfo=UTC),
            "review": '{"lesson": "ok"}',
            "review_error": None,
        }

    async def test_list_decision_summaries_maps_row(self) -> None:
        conn = _make_conn_mock()
        sid, scan = uuid4(), uuid4()
        conn.fetch = AsyncMock(return_value=[self._summary_row(sid, scan)])
        t = datetime(2026, 7, 1, tzinfo=UTC)
        rows = await DecisionRepository(conn).list_decision_summaries(
            since=t, until=t, after=None, limit=10
        )
        assert rows[0].id == sid
        assert rows[0].review == {"lesson": "ok"}
        assert rows[0].realized_r == 3.0
        assert rows[0].direction is SignalDirection.LONG
        assert rows[0].decision_kind == "PUBLISHED"
        sql, *args = conn.fetch.await_args.args
        assert "LEFT JOIN decision_resolutions r" in sql
        assert "LEFT JOIN decision_reviews v" in sql
        assert "CASE WHEN v.error IS NULL THEN v.review END AS review" in sql
        assert args[0] == t and args[1] == t
        assert args[2] is None and args[3] is None and args[4] == 10

    async def test_list_decision_summaries_unresolved_row_has_nulls(self) -> None:
        conn = _make_conn_mock()
        sid, scan = uuid4(), uuid4()
        row = self._summary_row(sid, scan)
        row.update(
            {
                "direction": None,
                "status": "SKIPPED",
                "violated_rule": "RULE_2_MIN_RR",
                "outcome": None,
                "decision_kind": None,
                "resolution_outcome": None,
                "realized_r": None,
                "entered_at": None,
                "review": None,
                "review_error": None,
            }
        )
        conn.fetch = AsyncMock(return_value=[row])
        rows = await DecisionRepository(conn).list_decision_summaries(
            since=T0, until=T0, after=None, limit=10
        )
        assert rows[0].review is None
        assert rows[0].violated_rule == "RULE_2_MIN_RR"
        assert rows[0].decision_kind is None

    async def test_list_decision_summaries_errored_review_is_none_with_error(self) -> None:
        conn = _make_conn_mock()
        row = self._summary_row(uuid4(), uuid4())
        # The SQL CASE nulls `review` when `error` is set; the mapper must not
        # try to parse a NULL and must surface the error text.
        row.update({"review": None, "review_error": "timeout"})
        conn.fetch = AsyncMock(return_value=[row])
        rows = await DecisionRepository(conn).list_decision_summaries(
            since=T0, until=T0, after=None, limit=10
        )
        assert rows[0].review is None
        assert rows[0].review_error == "timeout"

    async def test_list_decision_summaries_binds_keyset_cursor(self) -> None:
        conn = _make_conn_mock()
        conn.fetch.return_value = []
        cursor_id = uuid4()
        cursor_ts = datetime(2026, 7, 1, 2, tzinfo=UTC)
        await DecisionRepository(conn).list_decision_summaries(
            since=T0, until=T0, after=(cursor_ts, cursor_id), limit=10
        )
        sql, *args = conn.fetch.await_args.args
        assert "(s.created_at, s.id) > ($3, $4)" in sql
        assert "ORDER BY s.created_at ASC, s.id ASC" in sql
        assert args == [T0, T0, cursor_ts, cursor_id, 10]

    async def test_list_scan_runs_between_maps_and_binds(self) -> None:
        conn = _make_conn_mock()
        scan_id = uuid4()
        conn.fetch.return_value = [
            {
                "id": scan_id,
                "started_at": T0,
                "completed_at": None,
                "status": ScanStatus.SUCCESS.value,
                "error_message": None,
                "session": "LONDON",
                "strategy": "smc",
                "symbols": ["BTCUSDT"],
            }
        ]
        until = datetime(2026, 7, 8, tzinfo=UTC)
        rows = await DecisionRepository(conn).list_scan_runs_between(
            since=T0, until=until, after=None, limit=100
        )
        assert isinstance(rows[0], StoredScanRun)
        assert rows[0].id == scan_id
        sql, *args = conn.fetch.await_args.args
        assert "FROM scan_runs" in sql
        assert "(started_at, id) > ($3, $4)" in sql
        assert "ORDER BY started_at ASC, id ASC" in sql
        assert args == [T0, until, None, None, 100]

    async def test_list_active_setups_maps_and_binds_cursor(self) -> None:
        conn = _make_conn_mock()
        setup_id, sid = uuid4(), uuid4()
        conn.fetch.return_value = [
            {
                "id": setup_id,
                "signal_id": sid,
                "opened_at": T0,
                "status": ActiveSetupStatus.OPEN.value,
                "last_evaluated_at": None,
                "latest_evaluation": json.dumps({"verdict": "STILL_VALID"}),
            }
        ]
        cursor = (datetime(2026, 6, 30, tzinfo=UTC), uuid4())
        rows = await DecisionRepository(conn).list_active_setups(after=cursor, limit=50)
        assert isinstance(rows[0], StoredActiveSetup)
        assert rows[0].latest_evaluation == {"verdict": "STILL_VALID"}
        sql, *args = conn.fetch.await_args.args
        assert "FROM active_setups" in sql
        assert "(opened_at, id) > ($1, $2)" in sql
        assert "ORDER BY opened_at ASC, id ASC" in sql
        assert args == [cursor[0], cursor[1], 50]

    async def test_save_critic_report_inserts_all_columns(self) -> None:
        conn = _make_conn_mock()
        report = StoredCriticReport(
            id=uuid4(),
            period_start=T0,
            period_end=datetime(2026, 7, 8, tzinfo=UTC),
            report={"summary": "s"},
            markdown="# Critic",
            pr_url=None,
            created_at=datetime(2026, 7, 8, 1, tzinfo=UTC),
        )
        await DecisionRepository(conn).save_critic_report(report)
        sql, *args = conn.execute.await_args.args
        assert "INSERT INTO critic_reports" in sql
        assert args[0] == report.id
        assert args[1] == report.period_start
        assert args[2] == report.period_end
        assert json.loads(args[3]) == {"summary": "s"}
        assert args[4] == "# Critic"
        assert args[5] is None
        assert args[6] == report.created_at
