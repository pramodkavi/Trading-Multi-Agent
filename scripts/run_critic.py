"""Critic v0 Layer 2 CLI -- the GitHub Actions side of Step 2.14 (design §6, §7.1).

Layer 1 (``OutcomeResolver``) runs inside the scan Lambda because Binance
geofences GitHub's runners (HTTP 451). Everything that needs only Aurora (via
the RDS Data API) and Anthropic runs here, from ``.github/workflows/critic.yml``:

    python -m scripts.run_critic review --chunk-size 25 [--concurrency 4]
        One ``DecisionReviewer`` chunk. Last stdout line:
            REVIEW reviewed=<n> failed=<f> remaining=<k> cost_usd=<x.xxxx>
        Exit 0 even when individual reviews fail (they become error rows the
        queue retries); exit 2 only when the store / API is unreachable.

    python -m scripts.run_critic report --since <ISO> [--until <ISO>] [--exit-gate]
                                        --out <path.md> [--notify]
        One ``CriticReporter`` build; writes the markdown to ``--out``. Last line:
            REPORT id=<uuid> decisions=<n> gate=<PASS|FAIL|n/a>
        ``--notify`` posts the Telegram summary. Exit 0 on success, 2 on failure.

    python -m scripts.run_critic pending
        Size of the review queue, for the workflow to short-circuit:
            PENDING reviews=<k>

Contract with the workflow. The last stdout line of each command is parsed by
``critic.yml`` (``tail -n 1`` + ``sed``), so it is emitted exactly once, only on
success, and nothing else is written to stdout: logging goes to stderr. Every
failure path returns exit code 2 (argparse usage errors included) so a shell
``set -e`` stops the job.

Settings are loaded exactly like ``run_scan._load_settings``: hydrate secrets
from SSM Parameter Store first, then build the cached ``Settings``. The workflow
puts the secret *values* straight into the environment (no ``*_PARAM_NAME``),
which makes the hydration a no-op there; locally the same code reads ``.env``.
Resources (store, Anthropic client, notifier) are closed in ``finally`` blocks
like ``run_scan._run_forecaster`` so a failing command never leaks a connection.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from anthropic import AsyncAnthropic

from src.agents.critic.report import CriticReporter
from src.agents.critic.reviewer import (
    FAILED_REVIEW_RETRY_AFTER,
    NO_SETUP_MIN_AGE,
    DecisionReviewer,
)
from src.config import get_settings, hydrate_secrets_env
from src.notifications import TelegramNotifier, escape_markdown_v2
from src.persistence import create_store

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.agents.critic.models import ExitGateReport
    from src.config import Settings
    from src.persistence.models import StoredCriticReport

logger = logging.getLogger(__name__)

EXIT_OK: Final[int] = 0
EXIT_FAILURE: Final[int] = 2
"""Store / API unreachable, bad period, or any other failure -- also argparse's usage-error code."""

DEFAULT_CHUNK_SIZE: Final[int] = 25
DEFAULT_CONCURRENCY: Final[int] = 4

NOTIFY_SUMMARY_CHARS: Final[int] = 300
"""How much of ``CriticReport.summary`` the Telegram message carries (the PR has the rest)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested directly)
# ---------------------------------------------------------------------------


def _parse_iso(text: str) -> datetime:
    """Parse an ISO-8601 timestamp into an *aware* ``datetime``.

    Accepts a trailing ``Z`` (which ``datetime.fromisoformat`` only learned in
    3.11 -- normalised here explicitly for clarity) or a numeric offset. A naive
    timestamp is rejected rather than assumed UTC: the period bounds define
    which decisions a report covers, so an ambiguous bound is an error.
    """
    candidate = text.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{text!r} has no timezone; use a trailing Z or an explicit offset")
    return parsed


def _iso_arg(text: str) -> datetime:
    """argparse ``type=`` adapter: a ``ValueError`` becomes a usage error (exit 2)."""
    try:
        return _parse_iso(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value}")
    return value


def _gate_label(exit_gate: ExitGateReport | None) -> str:
    if exit_gate is None:
        return "n/a"
    return "PASS" if exit_gate.all_passed else "FAIL"


def _build_notification(stored: StoredCriticReport, *, exit_gate: ExitGateReport | None) -> str:
    """The Telegram summary for a saved report (plan Task 13), MarkdownV2-escaped.

    ``stored.report`` is the ``CriticReport`` JSON, so ``summary`` and
    ``proposed_changes`` are read from the dict the row actually holds. The
    whole message is built as plain text and escaped in one pass -- the
    ``TelegramNotifier`` sends with ``parse_mode=MarkdownV2`` and an unescaped
    ``.`` in the summary would make Telegram reject it with HTTP 400.
    """
    summary = str(stored.report.get("summary", ""))[:NOTIFY_SUMMARY_CHARS]
    changes = stored.report.get("proposed_changes") or []
    period = f"{stored.period_start:%Y-%m-%d} → {stored.period_end:%Y-%m-%d}"
    text = f"🧪 Critic report {period}: {summary} … {len(changes)} proposed changes"
    if exit_gate is not None:
        text += f" … gate {_gate_label(exit_gate)}"
    return escape_markdown_v2(text)


def _write_markdown(out: Path, markdown: str) -> None:
    """Write the report body, creating parents (``docs/critic-reports/`` may not exist yet).

    Blocking file I/O, so ``_report`` runs it via ``asyncio.to_thread`` (the
    codebase pattern for sync I/O inside async code). Explicit UTF-8: the
    markdown carries ``—`` / ``→`` and the runner's default encoding must not
    decide whether the file is valid.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _load_settings() -> Settings:
    """Hydrate secrets from SSM Parameter Store, then build the cached ``Settings``.

    Same ordering rule as ``run_scan._load_settings``: ``get_settings`` is
    ``lru_cache``d, so the secret values must be in the environment first.
    """
    hydrate_secrets_env()
    return get_settings()


async def _review(settings: Settings, *, chunk_size: int, concurrency: int) -> int:
    store = await create_store(settings)
    try:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
        try:
            reviewer = DecisionReviewer(store=store, client=client, concurrency=concurrency)
            summary = await reviewer.run(chunk_size=chunk_size)
        finally:
            await client.close()
    finally:
        await store.aclose()
    print(
        f"REVIEW reviewed={summary.reviewed} failed={summary.failed} "
        f"remaining={summary.remaining} cost_usd={summary.cost_usd:.4f}"
    )
    return EXIT_OK


async def _report(
    settings: Settings,
    *,
    since: datetime,
    until: datetime,
    include_exit_gate: bool,
    out: Path,
    notify: bool,
) -> int:
    """Build + persist the period report, write the markdown, optionally notify.

    Telegram is reached only after the report row is saved and the file is
    written, and a notifier failure is logged rather than fatal: the workflow
    step that follows commits ``--out`` and opens the proposed-rules PR, which
    must not be lost because one chat message bounced.
    """
    store = await create_store(settings)
    try:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
        try:
            reporter = CriticReporter(store=store, client=client)
            built = await reporter.build(
                since=since, until=until, include_exit_gate=include_exit_gate
            )
        finally:
            await client.close()
    finally:
        await store.aclose()

    await asyncio.to_thread(_write_markdown, out, built.stored.markdown)
    logger.info("critic report %s written to %s", built.stored.id, out)

    if notify:
        notifier = TelegramNotifier(
            token=settings.telegram_bot_token.get_secret_value(),
            chat_id=settings.telegram_chat_id,
        )
        try:
            await notifier.send(_build_notification(built.stored, exit_gate=built.exit_gate))
            logger.info("telegram summary sent for critic report %s", built.stored.id)
        except Exception:
            logger.exception("telegram summary failed for critic report %s", built.stored.id)
        finally:
            await notifier.aclose()

    print(
        f"REPORT id={built.stored.id} decisions={built.decision_count} "
        f"gate={_gate_label(built.exit_gate)}"
    )
    return EXIT_OK


async def _pending(settings: Settings) -> int:
    """Print the review-queue size using the same cutoffs as ``DecisionReviewer.run``."""
    now = _utcnow()
    store = await create_store(settings)
    try:
        count = await store.count_pending_reviews(
            no_setup_before=now - NO_SETUP_MIN_AGE,
            retry_before=now - FAILED_REVIEW_RETRY_AFTER,
        )
    finally:
        await store.aclose()
    print(f"PENDING reviews={count}")
    return EXIT_OK


async def _amain(args: argparse.Namespace) -> int:
    """Dispatch one sub-command; any exception becomes exit code 2 with a logged traceback."""
    try:
        if args.command == "report":
            until: datetime = args.until if args.until is not None else _utcnow()
            if args.since >= until:
                logger.error(
                    "--since (%s) must be before --until (%s)",
                    args.since.isoformat(),
                    until.isoformat(),
                )
                return EXIT_FAILURE
        settings = _load_settings()
        if args.command == "review":
            return await _review(settings, chunk_size=args.chunk_size, concurrency=args.concurrency)
        if args.command == "report":
            return await _report(
                settings,
                since=args.since,
                until=until,
                include_exit_gate=args.exit_gate,
                out=args.out,
                notify=args.notify,
            )
        if args.command == "pending":
            return await _pending(settings)
        logger.error("unknown command %r", args.command)
        return EXIT_FAILURE
    except Exception:
        logger.exception("run_critic %s failed", args.command)
        return EXIT_FAILURE


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_critic",
        description="Critic v0 Layer 2: review decisions with Claude and build the period report.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging goes to stderr; stdout carries only the machine-readable result line.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    review = commands.add_parser("review", help="Review one chunk of the pending-review queue.")
    review.add_argument(
        "--chunk-size",
        type=_positive_int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Decisions per run (default {DEFAULT_CHUNK_SIZE}).",
    )
    review.add_argument(
        "--concurrency",
        type=_positive_int,
        default=DEFAULT_CONCURRENCY,
        help=f"Parallel Claude calls (default {DEFAULT_CONCURRENCY}).",
    )

    report = commands.add_parser("report", help="Build the period report and write its markdown.")
    report.add_argument(
        "--since",
        type=_iso_arg,
        required=True,
        help="Period start, ISO-8601 with Z or offset (inclusive).",
    )
    report.add_argument(
        "--until",
        type=_iso_arg,
        default=None,
        help="Period end, ISO-8601 with Z or offset (exclusive; default: now).",
    )
    report.add_argument(
        "--exit-gate",
        action="store_true",
        help="Also evaluate the Slice 2 exit-gate criteria (SPEC §5).",
    )
    report.add_argument(
        "--out", type=Path, required=True, help="Markdown output path (parents are created)."
    )
    report.add_argument(
        "--notify", action="store_true", help="Send the Telegram summary after saving."
    )

    commands.add_parser("pending", help="Print the size of the pending-review queue.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return asyncio.run(_amain(args))


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
