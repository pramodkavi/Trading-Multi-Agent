"""Unit tests for ``scripts.run_critic`` (Critic v0 Layer 2 CLI, plan Task 13) -- offline.

The store, the Anthropic client, the ``DecisionReviewer`` / ``CriticReporter``
and the Telegram notifier are all monkeypatched on the module, so no network,
no database and no LLM call. The stdout last lines are a contract with
``.github/workflows/critic.yml`` (it pipes them through ``tail -n 1``), so the
tests assert them byte-for-byte with ``capsys``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import scripts.run_critic as run_critic_module
from scripts.run_critic import _build_notification, _parse_iso, main
from src.agents.critic.exit_gate import evaluate_exit_gate
from src.agents.critic.models import ExitGateReport
from src.agents.critic.report import ReportBuild
from src.agents.critic.reviewer import ReviewSummary
from src.config import Settings
from src.persistence.models import StoredCriticReport

SINCE = datetime(2026, 7, 1, tzinfo=UTC)
UNTIL = datetime(2026, 7, 8, tzinfo=UTC)
NOW = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        anthropic_api_key="sk-ant-test",
        telegram_bot_token="123:ABC",
        database_url="postgresql://u:p@localhost:5433/db",
        telegram_chat_id="111",
        _env_file=None,
    )


def _report_payload(*, summary: str = "Two of three published signals won.") -> dict[str, Any]:
    return {
        "summary": summary,
        "key_patterns": ["Premium longs lose"],
        "skeptic_assessment": "Sample too small.",
        "judge_assessment": "Vetoes avoided one loss.",
        "gate_assessment": "Both rejections avoided a loss.",
        "proposed_changes": [
            {
                "target": "JUDGE_SYSTEM_PROMPT",
                "change": "Weight HIGH-severity Skeptic objections only when macro data is fresh.",
                "rationale": "Respected win rate 100% vs overruled 0% on n=2.",
                "evidence_signal_ids": [str(uuid4())],
            }
        ],
    }


def _stored(**overrides: Any) -> StoredCriticReport:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "period_start": SINCE,
        "period_end": UNTIL,
        "report": _report_payload(),
        "markdown": "# Critic report — 2026-07-01 → 2026-07-08\n\nbody\n",
        "pr_url": None,
        "created_at": NOW,
    }
    fields.update(overrides)
    return StoredCriticReport(**fields)


def _failing_gate() -> ExitGateReport:
    return evaluate_exit_gate(scan_runs=[], decisions=[], setups=[])


def _passing_gate() -> ExitGateReport:
    failing = _failing_gate()
    return ExitGateReport(
        criteria=[c.model_copy(update={"passed": True}) for c in failing.criteria],
        all_passed=True,
    )


class _Deps:
    """Everything ``run_critic`` builds, replaced by mocks and handed back for asserts."""

    def __init__(self) -> None:
        self.store = MagicMock()
        self.store.aclose = AsyncMock()
        self.store.count_pending_reviews = AsyncMock(return_value=7)
        self.client = MagicMock()
        self.client.close = AsyncMock()
        self.notifier = MagicMock()
        self.notifier.send = AsyncMock()
        self.notifier.aclose = AsyncMock()
        self.reviewer = MagicMock()
        self.reviewer.run = AsyncMock(
            return_value=ReviewSummary(reviewed=1, failed=0, remaining=4, cost_usd=0.0123)
        )
        self.reporter = MagicMock()
        self.reporter.build = AsyncMock(
            return_value=ReportBuild(stored=_stored(), exit_gate=None, decision_count=8)
        )
        self.create_store = AsyncMock(return_value=self.store)
        self.anthropic_cls = MagicMock(return_value=self.client)
        self.notifier_cls = MagicMock(return_value=self.notifier)
        self.reviewer_cls = MagicMock(return_value=self.reviewer)
        self.reporter_cls = MagicMock(return_value=self.reporter)


@pytest.fixture
def deps(monkeypatch: pytest.MonkeyPatch) -> _Deps:
    d = _Deps()
    monkeypatch.setattr(run_critic_module, "_load_settings", _settings)
    monkeypatch.setattr(run_critic_module, "create_store", d.create_store)
    monkeypatch.setattr(run_critic_module, "AsyncAnthropic", d.anthropic_cls)
    monkeypatch.setattr(run_critic_module, "TelegramNotifier", d.notifier_cls)
    monkeypatch.setattr(run_critic_module, "DecisionReviewer", d.reviewer_cls)
    monkeypatch.setattr(run_critic_module, "CriticReporter", d.reporter_cls)
    return d


def _last_line(capsys: pytest.CaptureFixture[str]) -> str:
    out = capsys.readouterr().out
    return out.rstrip("\n").splitlines()[-1]


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------


class TestParseIso:
    def test_zulu_suffix_is_utc(self) -> None:
        parsed = _parse_iso("2026-06-12T00:00:00Z")
        assert parsed == datetime(2026, 6, 12, tzinfo=UTC)
        assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)

    def test_offset_is_preserved(self) -> None:
        parsed = _parse_iso("2026-06-12T05:30:00+05:30")
        assert parsed.utcoffset() == timedelta(hours=5, minutes=30)
        assert parsed == datetime(2026, 6, 12, tzinfo=UTC)

    def test_fractional_seconds_accepted(self) -> None:
        assert _parse_iso("2026-06-12T00:00:00.250Z") == datetime(
            2026, 6, 12, 0, 0, 0, 250_000, tzinfo=UTC
        )

    def test_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            _parse_iso("2026-06-12T00:00:00")

    def test_garbage_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            _parse_iso("last tuesday")


# ---------------------------------------------------------------------------
# _build_notification (pure)
# ---------------------------------------------------------------------------


class TestBuildNotification:
    def test_contains_period_summary_and_change_count(self) -> None:
        text = _build_notification(_stored(), exit_gate=None)
        assert text.startswith("🧪 Critic report 2026\\-07\\-01 → 2026\\-07\\-08: ")
        assert "Two of three published signals won\\." in text
        assert "1 proposed changes" in text
        assert "gate" not in text

    def test_summary_is_truncated_to_300_chars(self) -> None:
        long_summary = "x" * 400
        text = _build_notification(
            _stored(report=_report_payload(summary=long_summary)), exit_gate=None
        )
        assert "x" * 300 in text
        assert "x" * 301 not in text

    def test_markdown_v2_reserved_characters_are_escaped(self) -> None:
        summary = "Rate 66.7% (n=3) *bold* [link] _under_ score!"
        text = _build_notification(_stored(report=_report_payload(summary=summary)), exit_gate=None)
        assert "66\\.7% \\(n\\=3\\) \\*bold\\* \\[link\\] \\_under\\_ score\\!" in text

    def test_gate_verdict_appended_when_present(self) -> None:
        assert _build_notification(_stored(), exit_gate=_passing_gate()).endswith("gate PASS")
        assert _build_notification(_stored(), exit_gate=_failing_gate()).endswith("gate FAIL")


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


class TestReview:
    def test_prints_contract_line_and_exits_zero(
        self, deps: _Deps, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["review", "--chunk-size", "25"])

        assert code == 0
        assert _last_line(capsys) == "REVIEW reviewed=1 failed=0 remaining=4 cost_usd=0.0123"
        deps.reviewer.run.assert_awaited_once_with(chunk_size=25)

    def test_wires_store_client_and_concurrency(self, deps: _Deps) -> None:
        main(["review", "--chunk-size", "10", "--concurrency", "2"])

        deps.anthropic_cls.assert_called_once_with(api_key="sk-ant-test")
        deps.reviewer_cls.assert_called_once_with(
            store=deps.store, client=deps.client, concurrency=2
        )
        deps.reviewer.run.assert_awaited_once_with(chunk_size=10)

    def test_defaults_chunk_25_concurrency_4(self, deps: _Deps) -> None:
        main(["review"])

        deps.reviewer_cls.assert_called_once_with(
            store=deps.store, client=deps.client, concurrency=4
        )
        deps.reviewer.run.assert_awaited_once_with(chunk_size=25)

    def test_closes_store_and_client(self, deps: _Deps) -> None:
        main(["review"])

        deps.store.aclose.assert_awaited_once()
        deps.client.close.assert_awaited_once()
        deps.notifier_cls.assert_not_called()

    def test_store_exception_exits_two_and_logs(
        self, deps: _Deps, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        deps.reviewer.run = AsyncMock(side_effect=RuntimeError("aurora unreachable"))

        with caplog.at_level(logging.ERROR):
            code = main(["review"])

        assert code == 2
        assert "aurora unreachable" in caplog.text
        assert "REVIEW " not in capsys.readouterr().out
        deps.store.aclose.assert_awaited_once()
        deps.client.close.assert_awaited_once()

    def test_create_store_failure_exits_two(self, deps: _Deps) -> None:
        deps.create_store.side_effect = ValueError("bad backend")

        assert main(["review"]) == 2

    @pytest.mark.parametrize(
        "argv", [["review", "--chunk-size", "0"], ["review", "--concurrency", "0"]]
    )
    def test_non_positive_counts_are_usage_errors(self, deps: _Deps, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(argv)
        assert exc_info.value.code == 2
        deps.create_store.assert_not_awaited()


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _report_argv(out: Path, *extra: str) -> list[str]:
    return [
        "report",
        "--since",
        "2026-07-01T00:00:00Z",
        "--until",
        "2026-07-08T00:00:00Z",
        "--out",
        str(out),
        *extra,
    ]


class TestReport:
    def test_writes_markdown_and_prints_contract_line(
        self, deps: _Deps, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stored = _stored()
        deps.reporter.build = AsyncMock(
            return_value=ReportBuild(stored=stored, exit_gate=_passing_gate(), decision_count=8)
        )
        out = tmp_path / "r.md"

        code = main(_report_argv(out, "--exit-gate"))

        assert code == 0
        assert out.read_text(encoding="utf-8") == stored.markdown
        assert _last_line(capsys) == f"REPORT id={stored.id} decisions=8 gate=PASS"
        deps.reporter_cls.assert_called_once_with(store=deps.store, client=deps.client)
        deps.reporter.build.assert_awaited_once_with(
            since=SINCE, until=UNTIL, include_exit_gate=True
        )
        deps.notifier_cls.assert_not_called()

    def test_gate_fail_and_na(
        self, deps: _Deps, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        deps.reporter.build = AsyncMock(
            return_value=ReportBuild(stored=_stored(), exit_gate=_failing_gate(), decision_count=0)
        )
        assert main(_report_argv(tmp_path / "a.md", "--exit-gate")) == 0
        assert _last_line(capsys).endswith("decisions=0 gate=FAIL")

        deps.reporter.build = AsyncMock(
            return_value=ReportBuild(stored=_stored(), exit_gate=None, decision_count=3)
        )
        assert main(_report_argv(tmp_path / "b.md")) == 0
        assert _last_line(capsys).endswith("decisions=3 gate=n/a")
        deps.reporter.build.assert_awaited_once_with(
            since=SINCE, until=UNTIL, include_exit_gate=False
        )

    def test_creates_missing_parent_directories(self, deps: _Deps, tmp_path: Path) -> None:
        out = tmp_path / "docs" / "critic-reports" / "2026-07-08.md"

        assert main(_report_argv(out)) == 0
        assert out.is_file()

    def test_notify_sends_once_and_closes_notifier(self, deps: _Deps, tmp_path: Path) -> None:
        stored = _stored()
        deps.reporter.build = AsyncMock(
            return_value=ReportBuild(stored=stored, exit_gate=_passing_gate(), decision_count=8)
        )

        code = main(_report_argv(tmp_path / "r.md", "--exit-gate", "--notify"))

        assert code == 0
        deps.notifier_cls.assert_called_once_with(token="123:ABC", chat_id="111")
        deps.notifier.send.assert_awaited_once_with(
            _build_notification(stored, exit_gate=_passing_gate())
        )
        deps.notifier.aclose.assert_awaited_once()

    def test_notify_failure_is_logged_but_report_still_succeeds(
        self,
        deps: _Deps,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The report row is saved and the file written before Telegram is
        # reached; the workflow must still commit it and open the PR.
        deps.notifier.send = AsyncMock(side_effect=RuntimeError("telegram 502"))
        out = tmp_path / "r.md"

        with caplog.at_level(logging.ERROR):
            code = main(_report_argv(out, "--notify"))

        assert code == 0
        assert out.is_file()
        assert "telegram 502" in caplog.text
        assert _last_line(capsys).startswith("REPORT id=")
        deps.notifier.aclose.assert_awaited_once()

    def test_until_defaults_to_now(self, deps: _Deps, tmp_path: Path) -> None:
        before = datetime.now(UTC)
        code = main(["report", "--since", "2026-07-01T00:00:00Z", "--out", str(tmp_path / "r.md")])
        after = datetime.now(UTC)

        assert code == 0
        kwargs = deps.reporter.build.await_args.kwargs
        assert kwargs["since"] == SINCE
        assert before <= kwargs["until"] <= after
        assert kwargs["until"].tzinfo is not None

    def test_since_not_before_until_exits_two(
        self, deps: _Deps, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        argv = [
            "report",
            "--since",
            "2026-07-08T00:00:00Z",
            "--until",
            "2026-07-01T00:00:00Z",
            "--out",
            str(tmp_path / "r.md"),
        ]
        with caplog.at_level(logging.ERROR):
            code = main(argv)

        assert code == 2
        assert "--since" in caplog.text
        deps.create_store.assert_not_awaited()

    def test_offset_since_is_normalised_to_aware_datetime(
        self, deps: _Deps, tmp_path: Path
    ) -> None:
        main(
            [
                "report",
                "--since",
                "2026-07-01T05:30:00+05:30",
                "--until",
                "2026-07-08T00:00:00Z",
                "--out",
                str(tmp_path / "r.md"),
            ]
        )
        kwargs = deps.reporter.build.await_args.kwargs
        assert kwargs["since"] == SINCE
        assert kwargs["since"].tzinfo == timezone(timedelta(hours=5, minutes=30))

    def test_reporter_failure_exits_two_and_writes_nothing(
        self, deps: _Deps, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        deps.reporter.build = AsyncMock(side_effect=RuntimeError("api down"))
        out = tmp_path / "r.md"

        assert main(_report_argv(out)) == 2
        assert not out.exists()
        assert "REPORT " not in capsys.readouterr().out
        deps.store.aclose.assert_awaited_once()
        deps.client.close.assert_awaited_once()

    def test_missing_out_is_usage_error(self, deps: _Deps) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["report", "--since", "2026-07-01T00:00:00Z"])
        assert exc_info.value.code == 2

    def test_naive_since_is_usage_error(self, deps: _Deps, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["report", "--since", "2026-07-01T00:00:00", "--out", str(tmp_path / "r.md")])
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# pending
# ---------------------------------------------------------------------------


class TestPending:
    def test_prints_queue_size(self, deps: _Deps, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["pending"])

        assert code == 0
        assert _last_line(capsys) == "PENDING reviews=7"
        deps.store.aclose.assert_awaited_once()
        deps.anthropic_cls.assert_not_called()

    def test_uses_reviewer_queue_cutoffs(self, deps: _Deps) -> None:
        # Same predicate as DecisionReviewer.run: NO_SETUP rows older than 7
        # days, failed rows retried after 1 day -- both aware UTC datetimes.
        before = datetime.now(UTC)
        main(["pending"])
        after = datetime.now(UTC)

        kwargs = deps.store.count_pending_reviews.await_args.kwargs
        no_setup_before, retry_before = kwargs["no_setup_before"], kwargs["retry_before"]
        assert before - timedelta(days=7) <= no_setup_before <= after - timedelta(days=7)
        assert before - timedelta(days=1) <= retry_before <= after - timedelta(days=1)

    def test_store_failure_exits_two(self, deps: _Deps) -> None:
        deps.store.count_pending_reviews = AsyncMock(side_effect=RuntimeError("boom"))

        assert main(["pending"]) == 2
        deps.store.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def test_missing_subcommand_is_usage_error(deps: _Deps) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2
