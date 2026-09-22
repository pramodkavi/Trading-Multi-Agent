# Decision Review System (Critic v0) — Design

> **Status:** approved in design review 2026-09-22; implementation plan follows.
> **Closes:** SPEC §4 Step 2.14 (Slice 2 exit gate) and pulls SPEC Steps 3.5–3.7
> (Critic) forward in reduced form. A SPEC roadmap amendment is part of this work.
> **Authority:** `SPEC.md` remains authoritative. Where this design deviates from the
> SPEC roadmap (runtime, cadence, embeddings) the deviation is listed in §10 and must
> be written into SPEC in the same PR.

---

## 1. Goal

Take every trading decision the system has made, compare it with what the market
actually did afterwards, mark the decision correct or incorrect on hard price facts,
and only then let Claude judge *why* the reasoning was right or wrong and propose
prompt / rule changes.

Two consequences shape everything below:

1. **Ground truth is arithmetic, not opinion.** Whether price touched the target before
   the stop is computed from candles by a deterministic engine. No LLM is involved in
   producing an outcome.
2. **Nothing runs on the operator's machine.** Every step — backfill, steady-state, the
   AI review, the PR — runs in AWS Lambda or GitHub Actions.

## 2. What exists today and what is missing

| Fact | Consequence |
|---|---|
| Every proposal persists `direction`, `entry_price`, `stop_loss`, `take_profit_1`, `take_profit_2`, `created_at` (`signals.payload`). | Enough to replay each trade. |
| Judge-vetoed and gate-rejected proposals are persisted in full as the Analyzer `agent_runs.output`. | Skips can be scored counterfactually. |
| `signals.status` is derived from the payload *type*: any `SignalProposal` → `PUBLISHED`, including Judge vetoes (`src/persistence/repositories.py:230`, `store.py:483`). | `status` cannot be trusted for "was it published". Decision kind must come from the Judge's `agent_runs` ruling and the presence of an `active_setups` row. |
| The Forecaster writes `signals.outcome` **only** when Claude declares `INVALIDATED` (`src/agents/forecaster/forecaster.py:217`). WIN / LOSS are never computed by anyone. | The journal has no ground truth today. The Historian's win-rates are built on it. |
| `DataProvider.fetch_market_snapshot` returns only the most recent N candles; there is no start-time parameter. | A historical candle fetch must be added. |
| Every scheduled scan hardcodes `session=AD_HOC` (`scripts/run_scan.py:336`). | Rule 7 (session block) can never fire; the `session` column is useless for analysis. Recorded as a finding; **fixing it is out of scope here**. |
| GitHub-hosted runners are in US regions; Binance returns HTTP 451 there. | Candle fetching must run in the Mumbai Lambda, never in Actions. |
| The OIDC deploy role lacks `rds-data:ExecuteStatement`, `secretsmanager:GetSecretValue`, `cloudformation:DescribeStacks` (PROJECT_STATE §7). | Must be granted before the Actions side can read the journal. Also needs `lambda:InvokeFunction` on the scan Lambda and `ssm:GetParameter` on the Anthropic key. |

## 3. Vocabulary

**Decision** — one (scan, symbol) evaluation, i.e. one `signals` row joined to its
`agent_runs`. Classified as:

| Kind | Definition | Scored by price? |
|---|---|---|
| `PUBLISHED` | Judge `agent_runs.output.ruling ∈ {PUBLISH, PUBLISH_WITH_CAVEAT}` (equivalently an `active_setups` row exists) | Yes, real |
| `VETOED` | Payload is a `SignalProposal`, Judge ruling `SKIP` | Yes, counterfactual |
| `GATE_REJECTED` | Payload is a `SkipDecision` with non-null `violated_rule`; the proposal is in the Analyzer `agent_runs.output` | Yes, counterfactual |
| `NO_SETUP` | Payload is a `SkipDecision` with null `violated_rule` (Analyzer found nothing) | No |

**Resolution** — the deterministic price-replay result for a decision with a proposal.

**Review** — Claude's graded judgement of one decision, given its resolution.

**Critic report** — the aggregate over a period: patterns, agent value scores, proposed
changes. Delivered as a PR on `claude/proposed-rules-YYYY-MM-DD`.

## 4. Architecture

```
                 ┌──────────────── AWS (ap-south-1) ────────────────┐
                 │  Scan Lambda, new event mode {"mode":"resolve"}  │
  EventBridge ──▶│  OutcomeResolver: Aurora ─▶ Binance 5m candles   │
  rate(2 days)   │  ─▶ replay ─▶ signals.outcome / decision_resolutions │
                 └──────────────────────────────────────────────────┘
                                          ▲ lambda:InvokeFunction (backfill loop)
                                          │
                 ┌──────────────── GitHub Actions ──────────────────┐
  cron + dispatch│  critic.yml                                      │
                 │   1. (backfill) invoke resolve until remaining=0 │
                 │   2. DecisionReviewer: Aurora ─▶ Claude per      │
                 │      decision ─▶ decision_reviews                │
                 │   3. CriticReport: aggregate ─▶ markdown         │
                 │   4. open PR on claude/proposed-rules-<date>     │
                 │   5. Telegram summary                            │
                 └──────────────────────────────────────────────────┘
```

**Nothing runs on the operator's machine**: the backfill driver is the same Actions workflow
(`workflow_dispatch` with `mode=backfill`). Layer 1 (Resolver) lives in the Lambda because it needs Binance. Layer 2 (Reviewer +
Report) lives in Actions because it needs only Aurora, Anthropic and GitHub, and the PR
is opened with the workflow's own `GITHUB_TOKEN` (no PAT in Secrets Manager).

## 5. Layer 1 — Outcome Resolver (deterministic)

### 5.1 Work queue

`SELECT` decisions of kind `PUBLISHED | VETOED | GATE_REJECTED` that have no row in
`decision_resolutions`, oldest first, `LIMIT chunk_size` (default 25). The queue is the
data itself: idempotent and resumable with no cursor table. A decision whose replay
window has not yet closed and has not resolved is **not** written, so it is retried
next run.

### 5.2 Historical candles

New `DataProvider` method:

```python
async def fetch_klines(
    self, symbol: str, timeframe: Timeframe, *, start: datetime, end: datetime
) -> list[Kline]
```

`BinanceProvider` implements it by paging `fetch_ohlcv(symbol, tf, since=ms, limit=1000)`
until `end`, metering the existing `TokenBucket`. Candles are cached per (symbol, day)
in-process for the duration of one invocation so several decisions on the same symbol
and day share one fetch.

### 5.3 Replay rules

Inputs: proposal (`direction`, `entry_price`, `stop_loss`, `take_profit_1`,
`take_profit_2`), `decided_at = signals.created_at`, 5-minute candles from `decided_at`
to `decided_at + MAX_HOLD` (default **7 days**).

1. **Entry.** Walk candles from `decided_at`. Entry is *touched* when
   `low ≤ entry ≤ high`. If not touched within `ENTRY_VALIDITY` (default **72h**) →
   `EXPIRED`. If the very first candle already has price beyond entry in the trade's
   favour (a gap through), entry is treated as filled at that candle's open.
2. **After entry**, per candle in order:
   - stop touched: `LONG: low ≤ stop_loss`, `SHORT: high ≥ stop_loss`
   - target touched: `LONG: high ≥ take_profit_1`, `SHORT: low ≤ take_profit_1`
   - Stop first → `LOSS`. Target first → `WIN`. **Both in the same candle → `LOSS`**
     (conservative; intra-candle order unknown).
3. If neither is touched by `decided_at + MAX_HOLD` → `EXPIRED` with the mark-to-market
   R at the last candle recorded (metadata only; the outcome stays `EXPIRED`).
4. `BREAKEVEN` is **not** produced by v0 (no breakeven-stop logic exists in the system).
   `INVALIDATED` is reserved for the Forecaster and is never produced by the resolver.

Recorded per resolution:

| Field | Meaning |
|---|---|
| `outcome` | `WIN / LOSS / EXPIRED / UNRESOLVABLE` (the last when the proposal payload is missing or corrupt; never retried) |
| `entered_at`, `resolved_at` | timestamps; `entered_at` null if never entered |
| `realized_r` | `(exit − entry) / |entry − stop|` signed by direction; for EXPIRED-unentered = 0 |
| `max_adverse_excursion_r`, `max_favorable_excursion_r` | in R, after entry |
| `tp2_reached` | bool |
| `candles_used` | count, for audit |
| `engine_version` | constant string, bumped when rules change so old rows can be re-resolved |

### 5.4 Persistence

New table `decision_resolutions` (schema in §8). For `PUBLISHED` decisions the resolver
**also** writes `signals.outcome` and `signals.outcome_metadata` (overwriting any
Forecaster value; the resolver is the source of truth) and closes the `active_setups` row
with the matching status. For `VETOED` / `GATE_REJECTED` it writes only
`decision_resolutions` — counterfactual outcomes must never leak into the Historian's
retrieval, which filters on `PUBLISHED` rows with outcome.

### 5.5 Invocation contract

Event: `{"mode": "resolve", "chunk_size": 25}` → returns
`{"ok": true, "mode": "resolve", "resolved": n, "deferred": m, "remaining": k}` where
`remaining` is the number of unresolved decisions whose 7-day window has already closed
(deferred = window still open or provider error). Still-open decisions are not counted, so the
backfill loop terminates. The
backfill loop stops when `remaining == 0`. Per-decision provider errors are logged with
the existing `PROVIDER_ERROR` marker, counted in `deferred`, and never fail the batch.

## 6. Layer 2 — Decision Reviewer (Claude) and Critic Report

### 6.1 Reviewer work queue

Decisions with a `decision_resolutions` row and no `decision_reviews` row, plus
`NO_SETUP` decisions older than 7 days with no review row (they are reviewed without a
resolution, in aggregate-friendly form). Oldest first, `LIMIT chunk_size` (default 25).
Calls run with concurrency 4.

### 6.2 Per-decision review call

`structured_completion(output_schema=DecisionReview, ...)` with the existing helper;
model = Sonnet 4.5 (`DEFAULT_MODEL`). Prompt contents:

- the proposal (or skip) payload, verbatim
- Historian report, Skeptic objection, Judge decision (from `agent_runs`)
- the resolution row
- a compact post-decision price summary derived from the resolution (entered? when? MAE,
  MFE, outcome, R) — **no raw candles**, to keep tokens bounded

`DecisionReview` schema:

| Field | Type |
|---|---|
| `decision_id` | UUID (signals.id) |
| `smc_read_valid` | bool |
| `skeptic_vindicated` | `YES / NO / NOT_APPLICABLE` |
| `judge_correct` | bool |
| `gate_correct` | bool or null (GATE_REJECTED only) |
| `failure_category` | enum: `NONE, BAD_STRUCTURE_READ, WRONG_ZONE, RR_UNREALISTIC, IGNORED_SKEPTIC, OVERRULED_GOOD_SETUP, MACRO_MISSED, EXPIRED_NO_TRIGGER, OTHER` |
| `lesson` | str ≤ 300 chars |
| `confidence` | `LOW / MEDIUM / HIGH` |

Each review is stored in `decision_reviews` with `token_usage` and `cost_usd` from the
completion result, and also logged to `agent_runs` with `agent_role='critic'` so cost
tracking (NFR-2.3) works unchanged.

### 6.3 Aggregate report

When the review queue is empty for the period, one call over all reviews in the period
(`CriticReport` schema): win-rate by decision kind / symbol / rule / hour-of-day
(computed in Python, passed in), Skeptic value (win-rate when overruled vs respected),
Judge value (counterfactual outcome of vetoes), gate value (counterfactual outcome of
rejections), the top failure categories, and a list of `ProposedRuleChange` items each
naming a target: one of `SKEPTIC_SYSTEM_PROMPT`, `JUDGE_SYSTEM_PROMPT`,
`FORECASTER_SYSTEM_PROMPT`, `risk_gates.<constant>`, or `SPEC §1.6 rule N`, with the
proposed text and the evidence decision ids.

The report renders to markdown (`docs/critic-reports/YYYY-MM-DD.md`) and the
`ProposedRuleChange` list is written as a checklist into the same file. **v0 does not
edit prompts automatically**; the PR carries the proposals for human review, which is
the SPEC FR-2.2 contract.

### 6.4 Slice 2 exit-gate section

The first report (backfill) additionally renders a "Slice 2 exit gate" section computed
in Python from `scan_runs`, `signals` and `active_setups` (alarm history and AWS cost are
reviewed by the operator from the console; LLM cost comes from `agent_runs.cost_usd`), using the corrected queries from `docs/step-2.14-validation-plan.md`
review: 16 scan rows/day expected, published = has `active_setups` row, Forecaster
evidence = `active_setups.last_evaluated_at`, gate evidence = `SKIPPED` rows with
`violated_rule` (log cross-check noted as unavailable). Each criterion is PASS / FAIL
with evidence counts.

## 7. Orchestration

### 7.1 `critic.yml` (GitHub Actions)

```yaml
on:
  schedule: [{ cron: "30 22 */2 * *" }]   # every other day, after the 22:03 wrap scan
  workflow_dispatch:
    inputs:
      mode: { type: choice, options: [incremental, backfill], default: incremental }
      max_chunks: { default: "40" }
      period_start: { default: "" }        # ISO date; empty = last run watermark
permissions: { id-token: write, contents: write, pull-requests: write }
concurrency: { group: critic, cancel-in-progress: false }
```

Steps:

1. OIDC assume `AWS_DEPLOY_ROLE_ARN`, region from `vars.AWS_REGION`.
2. Resolve ARNs from the `CryptoSignals-Data` stack outputs (same shape as deploy-dev).
3. **Resolve loop** (both modes): `aws lambda invoke` with `{"mode":"resolve"}` until
   `remaining == 0` or `max_chunks` reached. In `incremental` mode this is normally 1–2
   invocations.
4. **Review loop**: `python -m scripts.run_critic review --chunk-size 25` until it prints
   `remaining=0` or `max_chunks` reached. The **first chunk in backfill mode stops the
   job** and prints the observed `cost_usd` per review and the extrapolated total; the
   operator re-dispatches with `mode=backfill` to continue. (Implemented as an input
   `confirm_cost: boolean`, default false; when false and mode=backfill, exit after one
   chunk.)
5. **Report**: `python -m scripts.run_critic report --since <period_start>` writes the
   markdown, commits to `claude/proposed-rules-<date>`, opens the PR via `gh pr create`
   with the report as body, and sends the Telegram summary.

The `*/2` day-of-month cron yields consecutive runs on the 31st and 1st in long months;
accepted.

### 7.2 EventBridge (steady-state resolver)

`SchedulingStack` gains `rate(2 days)` → scan Lambda with `{"mode":"resolve"}`. This
keeps outcomes flowing even if Actions is unavailable; the Actions resolve loop then
finds `remaining == 0` immediately.

### 7.3 Cost guard

Anthropic spend for the backfill is unknown until measured. The first-chunk stop in §7.1
step 4 is the guard: no more than 25 review calls happen before a human sees real numbers
(`agent_runs.cost_usd`). SPEC NFR-5.2's $200/month LLM budget is the ceiling; if the
extrapolation exceeds 25% of it, the operator reduces scope (e.g. skip `NO_SETUP`
reviews) before continuing.

## 8. Schema additions (`src/persistence/schema.sql`, idempotent)

```sql
CREATE TABLE IF NOT EXISTS decision_resolutions (
    signal_id                   UUID PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
    decision_kind               TEXT NOT NULL CHECK (decision_kind IN ('PUBLISHED','VETOED','GATE_REJECTED')),
    outcome                     TEXT NOT NULL CHECK (outcome IN ('WIN','LOSS','EXPIRED','UNRESOLVABLE')),
    entered_at                  TIMESTAMPTZ,
    resolved_at                 TIMESTAMPTZ NOT NULL,
    realized_r                  DOUBLE PRECISION NOT NULL,
    max_adverse_excursion_r     DOUBLE PRECISION,
    max_favorable_excursion_r   DOUBLE PRECISION,
    tp2_reached                 BOOLEAN NOT NULL DEFAULT FALSE,
    candles_used                INTEGER NOT NULL CHECK (candles_used >= 0),
    engine_version              TEXT NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS decision_reviews (
    signal_id       UUID PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
    decision_kind   TEXT NOT NULL CHECK (decision_kind IN ('PUBLISHED','VETOED','GATE_REJECTED','NO_SETUP')),
    review          JSONB NOT NULL,          -- DecisionReview.model_dump()
    model           TEXT NOT NULL,
    token_usage     JSONB NOT NULL DEFAULT '{}'::JSONB,
    cost_usd        NUMERIC(12,6),
    error           TEXT,                    -- set when the call failed; retried once
    attempts        INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS critic_reports (
    id              UUID PRIMARY KEY,
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    report          JSONB NOT NULL,          -- CriticReport.model_dump()
    markdown        TEXT NOT NULL,
    pr_url          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`SignalStore` gains: `list_unresolved_decisions`, `save_resolution`,
`list_unreviewed_decisions`, `save_review`, `list_reviews_since`, `save_critic_report`,
`list_scan_runs_since`, `list_agent_runs_for_scan`. Both backends (`AsyncpgSignalStore`,
`DataApiSignalStore`) implement all of them. Candle access stays on the provider side
(`fetch_klines`, §5.2), never on the store.

Because the CD migration step still skips (IAM), the schema is applied once via the
existing `{"mode":"migrate"}` Lambda path, invoked from the `critic.yml` job before the
first resolve loop (idempotent; harmless on later runs).

## 9. Code layout

```
src/agents/critic/
    __init__.py
    models.py          DecisionKind, DecisionRecord, Resolution, DecisionReview,
                       CriticReport, ProposedRuleChange, ExitGateReport
    inventory.py       build DecisionRecord from signals + agent_runs (pure)
    replay.py          resolve(proposal, decided_at, candles) -> Resolution (pure)
    resolver.py        OutcomeResolver: queue -> provider.fetch_klines -> replay -> store
    reviewer.py        DecisionReviewer: queue -> structured_completion -> store
    report.py          aggregates (pure) + CriticReport call + markdown render
    exit_gate.py       Slice 2 gate criteria (pure over fetched rows)
    prompts.py         REVIEW_SYSTEM_PROMPT, REPORT_SYSTEM_PROMPT
scripts/run_critic.py  CLI: review | report  (Actions side); resolve is a Lambda mode
scripts/run_scan.py    + {"mode":"resolve"} dispatch
src/providers/base.py  + fetch_klines abstract; binance.py implements
infrastructure/stacks/scheduling_stack.py  + rate(2 days) resolve schedule
.github/workflows/critic.yml
docs/critic-reports/   generated
```

## 10. SPEC amendments (same PR)

- §4 Step 2.14: closed by the first Critic v0 backfill report; exit-gate evidence is the
  report's gate section.
- §4 Steps 3.5–3.7: Critic v0 delivered early without pgvector clustering (3.4 stays in
  Slice 3 and plugs into `report.py`), runs in **GitHub Actions** not Lambda, cadence
  **every 2 days** not weekly Sunday; PR opening uses `GITHUB_TOKEN` (no Secrets Manager
  GitHub token, so the IAM line in 3.6 is dropped).
- §1.6 / Forecaster: the resolver is the source of truth for `WIN / LOSS / EXPIRED`; the
  Forecaster remains the source of `INVALIDATED` and of intra-trade alerts.
- New findings recorded for follow-up steps (not fixed here): Judge vetoes stored as
  `PUBLISHED`; `session` hardcoded to `AD_HOC` making rule 7 dead; no gate logging to
  CloudWatch.

## 11. Error handling

| Failure | Behaviour |
|---|---|
| Binance fetch error for one decision | log `PROVIDER_ERROR`, count as deferred, continue |
| Aurora resuming | existing `_execute` retry covers it |
| Claude call fails after retries | `decision_reviews.error` set, `review` = `{}`; retried once on the next run (query treats `error IS NOT NULL AND created_at < now()-1d` as unreviewed); surfaced in report |
| Actions job hits `max_chunks` | exits 0 with `::warning::`; next run continues |
| PR creation fails | report still committed to branch; job fails so the operator sees it |
| Telegram fails | logged, job does not fail |

## 12. Testing

- `replay.py`: exhaustive unit tests — long/short, entry never touched, gap-through entry,
  stop-first, target-first, both-in-one-candle → LOSS, TP2 reached, MAX_HOLD expiry, MAE/MFE
  arithmetic, empty candle list.
- `inventory.py`: decision-kind classification for all four kinds including the
  Judge-veto-as-PUBLISHED case.
- `exit_gate.py`, `report.py` aggregates: pure-function tests over fixture rows.
- Store additions: mirrored tests in `test_asyncpg_store.py` / `test_data_api_store.py`
  following existing fake-connection patterns; `test_migrate.py` table list updated.
- `BinanceProvider.fetch_klines`: paging and bucket metering with the existing fake
  exchange.
- `resolver.py`, `reviewer.py`: fake store + fake provider / fake Anthropic client (the
  `test_forecaster.py` / `test_judge.py` pattern); no network.
- `run_scan.py` resolve-mode dispatch; `run_critic.py` CLI exit codes and `remaining=`
  output contract.
- `tests/infra`: scheduling stack synthesizes the new schedule.
- Universal checkpoints (SPEC §5.1): ruff, ruff format, `mypy --strict src/ scripts/`,
  pytest all green, pre-commit.

## 13. Out of scope (explicit)

- Fixing the three findings in §10 (separate steps).
- Automatic prompt editing by the Critic.
- pgvector clustering (Step 3.4).
- Multi-timeframe / derivatives fetch in the scan path.
- Any order execution (permanent).
- Backtesting hypothetical strategies over history — only decisions actually made are
  scored.
