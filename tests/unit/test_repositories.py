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

from src.common.models import (
    AgentRole,
    ScanStatus,
    SignalDirection,
    SignalProposal,
    SignalStatus,
    SkipDecision,
    SkipReason,
)
from src.persistence import (
    AgentRunRepository,
    ScanRunRepository,
    SignalRepository,
    StoredAgentRun,
    StoredScanRun,
    StoredSignal,
)
from src.persistence.repositories import (
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
    """Build a mock asyncpg connection with the three async methods we use."""
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.fetch = AsyncMock()
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
