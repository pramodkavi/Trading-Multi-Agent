"""Enums shared across signal, scan, and judgment models.

These are foundation types referenced by every agent's I/O. Keeping them
in one module avoids circular imports and makes the vocabulary discoverable.
"""

from enum import StrEnum


class SignalDirection(StrEnum):
    """Direction of a proposed trade."""

    LONG = "LONG"
    SHORT = "SHORT"


class JudgeRuling(StrEnum):
    """Terminal decision of the Judge agent for a SignalProposal.

    PUBLISH               -- send to Telegram as a high-confidence signal.
    PUBLISH_WITH_CAVEAT   -- send to Telegram, prefixed with the Skeptic's
                             objection; recipient takes reduced size.
    SKIP                  -- do not publish; reasoning still journaled.
    """

    PUBLISH = "PUBLISH"
    PUBLISH_WITH_CAVEAT = "PUBLISH_WITH_CAVEAT"
    SKIP = "SKIP"


class ObjectionSeverity(StrEnum):
    """How strongly the Skeptic's macro objection argues against a proposal.

    Per SPEC §3.1.1 FR-1.5 the Skeptic emits a strongest-objection report with a
    severity rating. The Judge (Step 2.6) weighs this against the proposal and
    Historian evidence:

    LOW     -- a minor / contextual concern; macro is broadly neutral. The
               objection is the *best available*, not necessarily a real headwind.
    MEDIUM  -- a genuine macro headwind worth a published caveat.
    HIGH    -- macro strongly contradicts the setup; the Judge will likely SKIP.

    Note: macro UNAVAILABILITY is NOT a severity -- it is a separate NoMacroData
    result (FR-4.3) the Judge reads as "downgrade confidence to medium".
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class JudgeConfidence(StrEnum):
    """The Judge's confidence in its own ruling (SPEC §3.1.1 FR-1.6 / FR-4.3).

    Distinct from the ruling itself: the Judge can PUBLISH at LOW confidence
    (with a caveat) or SKIP at HIGH confidence. Per FR-4.3, when the Skeptic
    could not fetch macro context (NoMacroData) the Judge must cap confidence at
    MEDIUM -- absence of macro data is a reason for caution, not an all-clear.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ScanSession(StrEnum):
    """UTC trading session windows used by the scheduler and risk gates.

    Derived from SPEC.md §1.7 and §1.6 rule 7. The ASIAN and COOLDOWN
    windows are hard-blocked for new signals; the others are scan triggers.
    """

    LONDON = "LONDON"  # cron 3 8 * * *
    NY = "NY"  # cron 3 13 * * *
    OVERLAP = "OVERLAP"  # cron 3 15 * * * (London-NY)
    DAILY_WRAP = "DAILY_WRAP"  # cron 3 22 * * *
    ASIAN = "ASIAN"  # 00:00-08:00 UTC (no new signals)
    COOLDOWN = "COOLDOWN"  # 21:00-00:00 UTC (no new signals)
    AD_HOC = "AD_HOC"  # manual / dev triggers


class SignalStatus(StrEnum):
    """Persisted status of a row in the `signals` table.

    Discriminates between PUBLISHED (a SignalProposal that reached the journal
    via PUBLISH or PUBLISH_WITH_CAVEAT) and SKIPPED (a SkipDecision logged so
    the Critic can later analyse non-actions, per SPEC §3.1.1 FR-1.7).

    Lifecycle states for live setups (OPEN / WIN / LOSS / INVALIDATED) live on
    the `active_setups` table introduced at Step 2.8 -- not here.
    """

    PUBLISHED = "PUBLISHED"
    SKIPPED = "SKIPPED"


class ScanStatus(StrEnum):
    """Persisted status of a row in the `scan_runs` table.

    Mirrors the CHECK constraint in schema.sql. RUNNING is the initial state;
    SUCCESS / FAILED are terminal.
    """

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class AgentRole(StrEnum):
    """The six agent roles referenced by `agent_runs.agent_role`.

    Mirrors the CHECK constraint in schema.sql. ANALYZER is the only one used
    in Slice 1; the others come online in Slice 2 Steps 2.4-2.10 (Historian,
    Skeptic, Judge, Forecaster) and Slice 3 Step 3.5 (Critic).
    """

    ANALYZER = "analyzer"
    HISTORIAN = "historian"
    SKEPTIC = "skeptic"
    JUDGE = "judge"
    FORECASTER = "forecaster"
    CRITIC = "critic"


class SignalOutcome(StrEnum):
    """Terminal result of a published setup, written to `signals.outcome`.

    Set by the Forecaster (Step 2.9) when an active setup closes; NULL until then
    and for SKIPPED rows. Drives the Historian's outcome-aware retrieval and the
    Critic's win/loss analysis. Mirrors the CHECK constraint in schema.sql.
    """

    WIN = "WIN"  # take-profit reached
    LOSS = "LOSS"  # stop-loss hit
    BREAKEVEN = "BREAKEVEN"  # closed at/near entry
    INVALIDATED = "INVALIDATED"  # setup premise broke before triggering
    EXPIRED = "EXPIRED"  # never triggered within its validity window
