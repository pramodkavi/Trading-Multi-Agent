"""Contract tests for ``.github/workflows/critic.yml`` (Critic v0 design §7.1).

The workflow is orchestration, not Python, so the test pins the parts the rest
of the system depends on: the every-other-day cron after the 22:03 UTC wrap
scan, the ``workflow_dispatch`` inputs the runbook documents, the permissions
the ``gh pr create`` step needs, the two chunked loops, and the cost guard that
stops a backfill after its first review chunk unless ``confirm_cost`` is set.

It also pins the dashboard SPA contract (Slice 4 Step 4.5) in ``ci.yml`` and the
two deploy workflows: a ``frontend`` CI job, and the SPA built before any synth
or ``cdk deploy`` (``infrastructure/app.py`` refuses to synth without it).
"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# Dashboard SPA (Slice 4 Step 4.5): CI frontend job + SPA built before synth/deploy
# ---------------------------------------------------------------------------
#
# ``infrastructure/app.py`` refuses to synth unless ``dashboard-ui/dist/index.html``
# exists (``resolve_spa_dist``), so every job that runs ``python app.py`` or
# ``cdk deploy``/``cdk destroy`` must build the SPA first.

WORKFLOWS = REPO / ".github" / "workflows"
SPA_DIR = "dashboard-ui"
SPA_LOCK = "dashboard-ui/package-lock.json"


def _wf(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return doc


def _job_steps(name: str, job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _wf(name)["jobs"][job]["steps"]
    return steps


def _index(steps: list[dict[str, Any]], pred: Any) -> int:
    return next(i for i, s in enumerate(steps) if pred(s))


def _is_spa_build(step: dict[str, Any]) -> bool:
    run = step.get("run") or ""
    return step.get("working-directory") == SPA_DIR and "npm ci" in run and "npm run build" in run


def _setup_node(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-node@")]


def _assert_node_20_with_npm_cache(steps: list[dict[str, Any]]) -> None:
    nodes = _setup_node(steps)
    assert len(nodes) == 1, "exactly one setup-node step per job (reuse it, don't add a second)"
    with_ = nodes[0]["with"]
    assert with_["node-version"] == "20"
    assert with_["cache"] == "npm"
    assert with_["cache-dependency-path"] == SPA_LOCK


def _assert_spa_built_before(steps: list[dict[str, Any]], target: int) -> None:
    build = _index(steps, _is_spa_build)
    node = steps.index(_setup_node(steps)[0])
    assert node < build < target


def test_ci_frontend_job_runs_lint_typecheck_test_build() -> None:
    job = _wf("ci.yml")["jobs"]["frontend"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["defaults"]["run"]["working-directory"] == SPA_DIR
    steps: list[dict[str, Any]] = job["steps"]
    _assert_node_20_with_npm_cache(steps)
    runs = [(s.get("run") or "").strip() for s in steps]
    order = ["npm ci", "npm run lint", "npm run typecheck", "npm test", "npm run build"]
    positions = [runs.index(cmd) for cmd in order]
    assert positions == sorted(positions), f"frontend steps out of order: {positions}"
    assert steps.index(_setup_node(steps)[0]) < positions[0]


def test_ci_frontend_tests_run_once_not_in_watch_mode() -> None:
    package = json.loads((REPO / SPA_DIR / "package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["test"] == "vitest run"


def test_ci_synth_builds_spa_first() -> None:
    steps = _job_steps("ci.yml", "quality")
    _assert_node_20_with_npm_cache(steps)
    synth = _index(steps, lambda s: "python app.py" in (s.get("run") or ""))
    _assert_spa_built_before(steps, synth)


def test_ci_deps_scan_does_not_synth() -> None:
    runs = " ".join(s.get("run") or "" for s in _job_steps("ci.yml", "deps-scan"))
    assert "app.py" not in runs and "cdk " not in runs


def _first_cdk_step(steps: list[dict[str, Any]]) -> int:
    return _index(
        steps,
        lambda s: any(c in (s.get("run") or "") for c in ("cdk deploy", "cdk destroy")),
    )


def test_deploy_dev_builds_spa_before_cdk_deploy() -> None:
    steps = _job_steps("deploy-dev.yml", "deploy")
    _assert_node_20_with_npm_cache(steps)
    _assert_spa_built_before(steps, _first_cdk_step(steps))


def test_deploy_prod_builds_spa_before_cdk_deploy() -> None:
    steps = _job_steps("deploy-prod.yml", "deploy")
    _assert_node_20_with_npm_cache(steps)
    _assert_spa_built_before(steps, _first_cdk_step(steps))


def test_workflows_install_spa_deps_with_npm_ci_never_npm_install() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        # The global CDK CLI install (`npm install -g aws-cdk@<pin>`) is the only allowed
        # `npm install`; project deps always come from the lockfile via `npm ci`.
        for line in text.splitlines():
            if "npm install" in line and not line.lstrip().startswith("#"):
                assert "npm install -g aws-cdk@" in line, f"{path.name}: {line.strip()}"
        if SPA_DIR in text:
            assert "npm ci" in text, path.name


def test_critic_workflow_does_not_need_the_spa() -> None:
    text = (WORKFLOWS / "critic.yml").read_text(encoding="utf-8")
    assert "app.py" not in text and "cdk deploy" not in text and SPA_DIR not in text
