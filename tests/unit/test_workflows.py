"""Contract tests for ``.github/workflows/critic.yml`` (Critic v0 design §7.1).

The workflow is orchestration, not Python, so the test pins the parts the rest
of the system depends on: the every-other-day cron after the 22:03 UTC wrap
scan, the ``workflow_dispatch`` inputs the runbook documents, the permissions
the ``gh pr create`` step needs, the two chunked loops, and the cost guard that
stops a backfill after its first review chunk unless ``confirm_cost`` is set.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from scripts.run_critic import _parse_iso

REPO = Path(__file__).resolve().parents[2]
WF = REPO / ".github" / "workflows" / "critic.yml"
RUNBOOK = REPO / "docs" / "operations.md"


def _load() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(WF.read_text(encoding="utf-8"))
    return doc


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    on: dict[str, Any] = doc.get("on") or doc[True]  # PyYAML parses bare `on:` as True
    return on


def _steps(doc: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = doc["jobs"]["critic"]["steps"]
    return steps


def _runs(doc: dict[str, Any]) -> list[str]:
    return [s.get("run") or "" for s in _steps(doc)]


def test_critic_workflow_parses_and_has_contract() -> None:
    doc = _load()
    on = doc.get("on") or doc.get(True)  # PyYAML parses bare `on:` as True
    assert on["schedule"][0]["cron"] == "30 22 */2 * *"
    assert set(on["workflow_dispatch"]["inputs"]) == {
        "mode",
        "confirm_cost",
        "max_chunks",
        "period_start",
    }
    assert doc["permissions"] == {
        "id-token": "write",
        "contents": "write",
        "pull-requests": "write",
    }
    steps = _steps(doc)
    names = [s.get("name", "") for s in steps]
    assert "Resolve loop (Lambda, Mumbai)" in names and "Review loop (Claude)" in names
    assert any('"mode":"resolve"' in (s.get("run") or "") for s in steps)
    assert any("scripts.run_critic review" in (s.get("run") or "") for s in steps)
    assert any("gh pr create" in (s.get("run") or "") for s in steps)


def test_critic_workflow_dispatch_inputs_default_to_the_safe_incremental_path() -> None:
    inputs = _on(_load())["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["type"] == "choice"
    assert inputs["mode"]["options"] == ["incremental", "backfill"]
    assert inputs["mode"]["default"] == "incremental"
    assert inputs["confirm_cost"]["type"] == "boolean"
    assert inputs["confirm_cost"]["default"] is False
    assert inputs["max_chunks"]["default"] == "40"
    assert inputs["period_start"]["default"] == ""


def test_critic_workflow_runs_one_at_a_time_and_never_cancels_in_flight() -> None:
    doc = _load()
    assert doc["concurrency"] == {"group": "critic", "cancel-in-progress": False}
    assert doc["jobs"]["critic"]["runs-on"] == "ubuntu-latest"
    assert doc["jobs"]["critic"]["timeout-minutes"] >= 60


def test_critic_workflow_applies_schema_before_resolving() -> None:
    runs = _runs(_load())
    migrate = next(i for i, r in enumerate(runs) if '"mode":"migrate"' in r)
    resolve = next(i for i, r in enumerate(runs) if '"mode":"resolve"' in r)
    review = next(i for i, r in enumerate(runs) if "scripts.run_critic review" in r)
    report = next(i for i, r in enumerate(runs) if "scripts.run_critic report" in r)
    assert migrate < resolve < review < report


def test_critic_workflow_cost_guard_gates_the_report_step() -> None:
    steps = _steps(_load())
    review = next(s for s in steps if s.get("name") == "Review loop (Claude)")
    assert review["id"] == "review"
    assert "stopped_for_cost=true" in review["run"]
    assert "confirm_cost" in review["run"]
    report = next(s for s in steps if "scripts.run_critic report" in (s.get("run") or ""))
    assert report["if"] == "steps.review.outputs.stopped_for_cost != 'true'"
    # The exit gate is rendered only for a backfill; reports land under docs/critic-reports/.
    assert "--exit-gate" in report["run"]
    assert "docs/critic-reports/" in report["run"]
    assert "claude/proposed-rules-" in report["run"]
    assert "--notify" in report["run"]


def test_critic_workflow_resolve_loop_stops_on_drain_or_no_progress() -> None:
    steps = _steps(_load())
    resolve = next(s for s in steps if s.get("name") == "Resolve loop (Lambda, Mumbai)")
    run = resolve["run"]
    assert '"chunk_size":25' in run
    assert '["remaining"]' in run and '["resolved"]' in run
    assert "resolve queue drained" in run
    assert "::warning::no progress" in run


def test_critic_workflow_reads_secrets_from_ssm_and_masks_them() -> None:
    doc = _load()
    cfg = next(s for s in _steps(doc) if s.get("id") == "cfg")
    run = cfg["run"]
    assert "/crypto-signals/anthropic-api-key" in run
    assert "/crypto-signals/telegram-bot-token" in run
    assert run.count("::add-mask::") >= 2
    for var in ("DB_CLUSTER_ARN", "DB_SECRET_ARN", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN"):
        assert f"{var}=" in run
    # Settings (src/config/settings.py) read these names directly; no *_PARAM_NAME indirection.
    assert doc["env"]["PERSISTENCE_BACKEND"] == "dataapi"
    assert doc["env"]["DB_NAME"] == "signals"


def test_critic_workflow_uploads_the_report_artifact_even_on_failure() -> None:
    steps = _steps(_load())
    upload = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "docs/critic-reports/*.md"
    assert upload["with"]["if-no-files-found"] == "ignore"


# ---------------------------------------------------------------------------
# period_start -> run_critic report --since
# ---------------------------------------------------------------------------


def _report_run(doc: dict[str, Any]) -> str:
    step = next(s for s in _steps(doc) if s.get("name") == "Report + PR + Telegram")
    run: str = step["run"]
    return run


def _normalise_period_start(value: str) -> str:
    """Python mirror of the workflow's ``case "$START" in *T*) ;; *) ...`` rule."""
    return value if "T" in value else f"{value}T00:00:00Z"


def test_critic_workflow_normalises_a_bare_period_start_date_before_run_critic() -> None:
    """``period_start`` is an ISO *date*; ``run_critic report --since`` rejects naive input.

    ``_parse_iso`` (Task 13 contract) raises on anything without a ``Z`` / offset,
    so the workflow must widen a bare ``YYYY-MM-DD`` to ``YYYY-MM-DDT00:00:00Z``
    (and leave a full timestamp alone) *before* the ``--since`` call. Otherwise a
    runbook backfill burns the whole review budget and then fails at the report
    step with no PR.
    """
    doc = _load()
    run = _report_run(doc)
    normalise_at = run.index('case "$START" in')
    since_at = run.index('--since "$START"')
    assert normalise_at < since_at, "period_start must be normalised before it reaches --since"
    assert "*T*) ;;" in run, "a full ISO-8601 timestamp must pass through untouched"
    assert 'START="${START}T00:00:00Z"' in run, "a bare date must become UTC midnight"
    # The dispatch input must tell the operator exactly what the workflow accepts.
    description = _on(doc)["workflow_dispatch"]["inputs"]["period_start"]["description"]
    assert "YYYY-MM-DD" in description
    assert "UTC" in description


def test_critic_workflow_period_start_values_are_accepted_by_run_critic() -> None:
    """Every value the workflow or the runbook can feed ``--since`` must satisfy ``_parse_iso``."""
    run = _report_run(_load())
    # Defaults substituted when the input is empty.
    assert "START=2026-06-12T00:00:00Z" in run  # backfill: go-live
    assert "+%Y-%m-%dT00:00:00Z" in run  # incremental: 14 days ago, rendered by `date -u`
    assert _parse_iso("2026-06-12T00:00:00Z") == datetime(2026, 6, 12, tzinfo=UTC)

    # Both shapes the input description allows.
    assert _parse_iso(_normalise_period_start("2026-07-01")) == datetime(2026, 7, 1, tzinfo=UTC)
    full = "2026-07-01T09:30:00Z"
    assert _normalise_period_start(full) == full
    assert _parse_iso(full) == datetime(2026, 7, 1, 9, 30, tzinfo=UTC)

    # Every `-f period_start=<value>` example in the operator runbook (§7.2).
    examples = re.findall(r"-f period_start=(\S+)", RUNBOOK.read_text(encoding="utf-8"))
    assert examples, "docs/operations.md §7.2 must show how to pass period_start"
    for example in examples:
        parsed = _parse_iso(_normalise_period_start(example))
        assert parsed.utcoffset() == timedelta(0)
        if "T" not in example:  # a bare date means UTC midnight of that day
            assert (parsed.hour, parsed.minute, parsed.second) == (0, 0, 0)
