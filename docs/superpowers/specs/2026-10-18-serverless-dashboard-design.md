# Serverless Web Dashboard — Design

> **Status:** APPROVED by the operator 2026-10-19 (builder-checked; §14 decisions resolved).
> Implementation plan follows. Supersedes the Slice 4 architecture in
> SPEC §2.5 / §4 Steps 4.1–4.11; the SPEC amendments in §11 ship with the first
> implementation PR.
> **Authority:** `SPEC.md` remains authoritative. Where this design deviates from it, the
> deviation is listed in §11 and must be written into SPEC in the same commit.
> **Build discipline:** CLAUDE.md §6 applies — one step per session, tests before "done",
> branch per step (`feat/slice-4-step-M-...`), docs in the same commit.

---

## 1. Intent

Give the operator a private web dashboard that answers, from a phone or a laptop, the
questions they ask today by reading CloudWatch logs and Telegram history:

1. **Is the system alive and healthy right now?** (Production was silently degraded for six
   weeks in Aug–Oct 2026 — Forecaster failures were swallowed and no alarm fired. See
   `docs/PROJECT_STATE.md §2`.)
2. **Why is nothing being published?** (Since 2026-08-26 the Analyzer proposed on ~100% of
   scans and the risk gates rejected ~100%, dominated by one rule. Nobody could see that.)
3. **What is open, what did the system see, and what did each agent say?** (The Telegram
   alert is a summary; the full reasoning chain is in Postgres with no viewer.)
4. **Is the system any good?** (Win rate by tag/session/strategy, Skeptic value, the
   Critic's findings.)

The dashboard is a **window onto the system**, not a control room. It is **read-only** in
this design (§4.1 explains the call and what a later write phase would add).

## 2. Operating constraints (agreed with the operator)

| # | Constraint | Consequence |
|---|---|---|
| C1 | **Single user.** The operator is the only user, ever, for this phase. | Cognito user pool with one user; no roles; no WAF; no multi-tenant data shapes. |
| C2 | **Lowest possible cost.** Today's all-in bill is ~$5–9/month and must not materially increase. | No always-on compute. Nothing may hold a Postgres connection open (Aurora must keep scaling to zero). Pay-per-request services only. Target: **< $1/month** added at the operator's usage (§9). |
| C3 | **Align with the serverless pipeline.** Lambda (zip + layer, console-editable, outside the VPC), Aurora via the RDS Data API, SSM parameters, CDK, GitHub-Actions-only deploys. | The dashboard backend is the same packaging as the scan Lambda and reuses `DataApiSignalStore`. The SPEC's Fargate + ALB + WebSocket + LISTEN/NOTIFY design is **dropped** (§3.1). |
| C4 | **Signal-only, read/write decoupled (FR-7.4).** The dashboard never invokes the agent pipeline. | Phase 1 has no write endpoints at all. |
| C5 | **One environment.** dev = test = prod in account `097853039368` / `ap-south-1`. | One `DashboardStack`; no staging URL. |
| C6 | **Deploys via GitHub Actions only; no AWS CLI locally.** | The frontend build and S3 upload run in the existing deploy workflows. |

## 3. Architecture

### 3.1 Why the SPEC's Slice 4 shape is replaced

The SPEC assumed a long-running FastAPI service on Fargate so it could keep a Postgres
connection open for `LISTEN/NOTIFY` and push WebSocket updates. That shape costs about as
much as the entire pipeline does today (smallest always-on Fargate task ≈ $10/month, plus
it pins Aurora above zero ACU, plus an ALB), for a system whose state changes **about
eight times a day** (four scans + four Forecaster sweeps). The operator does not need
sub-second push; a page that re-fetches every 60 seconds and shows a "last updated" stamp
is indistinguishable in practice.

### 3.2 Components

```
Browser (React SPA)
   │  https://<cloudfront-domain>/            → S3 (static assets, OAC)
   │  https://<cloudfront-domain>/api/*       → API Gateway HTTP API (same origin: no CORS)
   │                                              └─ JWT authorizer (Cognito user pool)
   │                                                   └─ Dashboard Lambda (FastAPI via Mangum)
   │                                                         ├─ RDS Data API → Aurora (read-only SQL)
   │                                                         ├─ CloudWatch DescribeAlarms / GetMetricData (read)
   │                                                         └─ BinanceProvider (klines for charts)
   └─ Cognito Hosted UI (login, token refresh)
```

| Component | Choice | Why |
|---|---|---|
| Backend | **FastAPI app wrapped by Mangum**, one `lambda_.Function` (zip code asset + dependency layer via `infrastructure/lambda_assets.py`, Python 3.11, outside the VPC). | Same packaging and console-editability as the scan Lambda. Mangum is pure Python; FastAPI/Starlette/Mangum add ~10 MB to a layer. |
| API front door | **API Gateway HTTP API** (not REST API) with a **Cognito JWT authorizer** on every route except `/api/health`. | ~$1 per million requests (free tier covers the first year). The JWT check happens before the Lambda runs, so there is no auth code in Python and unauthenticated calls cost nothing. |
| Database access | **`DataApiSignalStore`** (existing) plus a new read-only query module. Reuses the existing `DatabaseResumingException` retry. | No VPC, no connection pool, Aurora still scales to zero. |
| Frontend | **React 18 + TypeScript + Vite**, Tailwind CSS, react-router, TanStack Query (polling), TradingView Lightweight Charts (price), Recharts (analytics). | What the SPEC already chose, minus WebSocket. |
| Hosting | **S3 bucket (private, OAC) + CloudFront** distribution. CloudFront also fronts `/api/*` so the SPA and API share one origin. | HTTPS for free; CloudFront free tier covers this traffic indefinitely. |
| Auth | **Cognito user pool**, one user created by the operator in the console, Hosted UI + authorization-code-with-PKCE via `react-oidc-context`. Access token in memory; refresh token in `localStorage`. Advanced security features **off** (they are the paid tier). | Free at one user. Meets NFR-3.4. |
| WAF | **Not deployed.** | Operator decision (C1). NFR-3.5 is amended in §11. |
| Real time | **None.** TanStack Query `refetchInterval` 60 s while the tab is visible; manual refresh button; "updated N s ago" stamp. | §3.1. |
| Market data | Backend endpoint `/api/market/klines` proxies through the existing `BinanceProvider`. | Keeps the "all external data through `DataProvider`" rule and avoids depending on Binance CORS or the operator's geolocation. The Lambda runs in ap-south-1 where Binance is reachable. |
| Infra | New **`DashboardStack`** in `infrastructure/app.py` (API Lambda + log group + HTTP API + authorizer + Cognito + S3 + CloudFront). Depends on `DataStack` (cluster grant). | One stack, destroyable independently. |

### 3.3 Dependency layer

The scan Lambda's layer is ~190 MB of a 240 MB guard. The dashboard Lambda gets its **own
layer** built from `[project].dependencies` **plus** a new `[project.optional-dependencies]
dashboard = ["fastapi", "mangum"]` extra, via the existing builder in
`infrastructure/lambda_assets.py` (`build_layer()` / `deps_layer()`; extend it to accept an
extras list, keeping the `boto3` / `psycopg` excludes). The scan Lambda's layer is unchanged.
Both layers share the existing size guards. (Builder: `ccxt` is still needed in the dashboard
layer because of `/api/market/klines`.)

**Settings:** `src/config/settings.py::Settings` requires the Anthropic and Telegram secrets.
The dashboard Lambda gets its own small `DashboardSettings` (DB ARNs, DB name, watchlist,
log level, optional Langfuse base URL for trace links) and never loads the agent `Settings`.

### 3.4 Aurora wake-up behaviour

The first query after idle hits `DatabaseResumingException` for ~10–15 s. The existing retry
in `DataApiSignalStore._execute` waits up to 75 s, which is longer than API Gateway's hard
30 s timeout. The dashboard therefore constructs the store with a **short resume budget**
(constructor parameter `resume_attempts=1`, shipped in Step 4.0; the scan Lambda keeps the default) and
returns **HTTP 503 with `{"reason":"db_resuming"}`** when that budget is exhausted. The
frontend shows a "waking the database…" state and retries with backoff for up to 60 s
before showing an error. The Lambda timeout is 25 s.

**Response size:** the RDS Data API caps one response at 1 MB. Every list endpoint is paged
server-side (default 50, max 200 rows), the journal's CSV export is assembled client-side
from pages, and the lifecycle payload returns agent outputs trimmed to the fields the
timeline renders, with the full JSON fetched per step on expand.

### 3.5 Deploy

- The deploy workflow builds the SPA first (`npm ci && npm run build` in `dashboard-ui/`,
  Node 20 on the runner), then `cdk deploy` creates/updates `DashboardStack` **last** in the
  stack order. The OIDC deploy role is **not** extended: the upload, the `config.json`, and
  the CloudFront invalidation all happen **inside the stack** via
  `aws_s3_deployment.BucketDeployment` with `Source.asset("dashboard-ui/dist")` plus
  `Source.json_data("config.json", {...})` built from the stack's own Cognito / API
  resources, and `distribution=` so the deployment invalidates on change. No stack-output
  round-trip, no two-pass build.
- **Schema changes** (Step 4.0's table and indexes) reach Aurora the way every schema change
  does today: the CD migration step skips because of the deploy role, so the operator
  invokes the scan Lambda with `{"mode":"migrate"}` from the console after the deploy
  (`docs/operations.md §2.1`). Fixing the role stays a separate §7 action item.
- Frontend lint/type-check/unit tests run in `ci.yml` on every PR.

## 4. Scope

### 4.1 Decision: read-only in this phase (my call — operator to confirm)

Two features from the brainstorm need a write path: an **execution journal** (did the
operator take the trade, at what fill, what did it actually make) and **operator controls**
(pause publishing, mute Telegram, trigger an ad-hoc scan, edit the watchlist). They are
**deferred to a Phase 2 design**, for three reasons: (a) FR-7.4 decoupling keeps the
dashboard incapable of harming the live pipeline; (b) every Phase 1 view is useful without
them; (c) controls need a pipeline-side flag read (`system_flags` table) that is a pipeline
change, not a dashboard change. The schema and API are laid out so Phase 2 adds
`POST` routes and two tables without touching Phase 1 (§6.3).

### 4.2 Pages (Phase 1)

Pages are listed in build priority. Each has its API endpoints in §6.

**P1 — Overview (landing page).** The "is it alive, what is open" screen, designed to be
readable on a phone in ten seconds.
- *Health strip:* one badge per concern — last scan window outcome (SUCCESS/FAILED/missed),
  Forecaster coverage ("all open setups evaluated in the last window" / "N stale"),
  CloudWatch alarm states (the 4 existing alarms), DB reachable. Any red badge links to
  the Health page.
- *Risk budget:* open setups vs cap 3; signals in last 24 h vs cap 5; loss-streak count and
  whether the 24 h pause is active; current session (London/NY/Overlap/Asian-blocked/
  Cooldown-blocked) and the next scheduled scan with a countdown. Computed by the same
  rules as `risk_gates.gather_risk_context`, so the numbers match what the gate will see.
- *Open setups:* one card per OPEN `active_setups` row — symbol, direction, entry/SL/TP,
  unrealised R from the latest price, Forecaster's latest verdict and age, link to the
  signal detail page.
- *Last scan per window:* the four windows for today with per-symbol outcome chips
  (no-setup / gate-rejected: rule / vetoed / published).

**P2 — Signal detail (lifecycle).** `/signals/{id}`; the target of the Telegram deep link
(§7). A vertical timeline: Analyzer proposal (or skip) → risk gate result (every violated
rule, not just the first) → Historian report (win rate, sample size, and the **list of
retrieved neighbour signals with their outcomes**, each linking to its own detail page) →
Skeptic objection or `NoMacroData` → Judge ruling, confidence, caveat → each Forecaster
evaluation in order → terminal outcome and `outcome_metadata`. Each step shows the raw
structured output (collapsible JSON) and, once populated, latency/tokens/cost. A chart
panel draws the H4 candles around `created_at` with entry/SL/TP1/TP2 lines and the POI
zone from the proposal.

**P3 — Signals journal.** Filterable, paginated table of `signals` joined to the decision
classification (§5.1): date range, symbol, decision kind, outcome, tag, session, violated
rule. CSV export of the current filter (client-side from the fetched page set; server
limit 5 000 rows).

**P4 — Decision funnel.** For a selectable period and grouping (day / window / symbol):
scans → symbols scanned → Analyzer proposals → gate passes (with a stacked bar of
rejections **by rule**) → Judge PUBLISH / PUBLISH_WITH_CAVEAT / SKIP → published. Plus a
"top rejection rules, last 7 / 30 days" table. This page would have exposed the Aug–Oct
problem in one glance.

**P5 — Live markets.** Four symbol tiles (watchlist from `Settings.scan_symbols` via the
API) each with: the Analyzer's most recent HTF bias and market phase for that symbol
(from the latest Analyzer `agent_runs` output), an H4 Lightweight-Charts candlestick chart,
overlays for any OPEN setup on that symbol (entry/SL/TP lines, POI zone), and the latest
funding/OI if the snapshot carried them. Candles come from `/api/market/klines`.

**P6 — Performance analytics** (FR-7.3 as written, plus two additions): win rate over time;
win rate by tag, by session, by strategy; Skeptic value (outcome when the Judge respected vs
overruled a HIGH/MEDIUM objection); **hypothetical R-multiple equity curve** (every
PUBLISHED signal at 1 R risk, using `outcome` and `outcome_metadata.realized_r` where
present, else +RR / −1 / 0); and, when the Critic v0 branch lands, the latest
`critic_reports` (markdown rendered, PR link) and `decision_reviews` summaries. Charts use
Recharts; follow the `dataviz` skill palette rules.

**P7 — System health.** The detail behind the Overview strip: scan history table (last 50
`scan_runs`: window, status, duration, symbols, error), per-agent run counts and failures
per day (from `agent_runs`), Forecaster coverage per sweep, the 4 CloudWatch alarms with
state and last transition, Lambda invocation/error/duration metrics for 7 days, and **LLM
cost per day** from `agent_runs.cost_usd` (see prerequisite in §5.2). Config panel: deployed
watchlist, scan schedule, model name, whether Langfuse / macro providers are configured.

### 4.3 Non-goals (this design)

- Any write path: trade journal, pause/mute, ad-hoc scan trigger, watchlist edit (Phase 2).
- WebSocket / push updates; LISTEN/NOTIFY triggers.
- WAF, geo-restriction, rate limiting beyond API Gateway defaults.
- Multi-user, roles, sharing links.
- Editing strategies / rules from the UI (Slice 3 strategy registry stays config-only).
- Rendering the Analyzer's full detected structure (every OB/FVG/liquidity pool) on the
  chart. The pipeline does not persist those zones today; only the proposal's entry/SL/TP and
  POI are available. Persisting the zone set is a pipeline change (§5.2, optional).
- Mobile app, PWA install prompts (the SPA must be **responsive**, which is enough).

### 4.4 Anti-goals (what would make this a failure even if it works)

- The bill goes up by more than ~$1/month at normal use (§9).
- The dashboard can affect a scan (shared mutable config, pipeline invocation, long locks).
- A broken dashboard deploy breaks or delays a pipeline deploy (keep `DashboardStack`
  last in the deploy order and independent of Compute/Scheduling/Monitoring).
- The Overview is unreadable on a phone.

## 5. Data model notes (what exists, what the dashboard must derive, what must change)

### 5.1 Decision classification is derived, not stored

`signals.status` is `PUBLISHED` for **every** `SignalProposal` payload, including Judge
vetoes (`store.create_signal`). The only trustworthy source for "was it actually published"
is the Judge's `agent_runs` row for that scan + symbol. The Critic v0 branch
(`feat/slice-2-step-2.14-critic-v0`, `src/agents/critic/inventory.py`) already implements
this stitching into `PUBLISHED / VETOED / GATE_REJECTED / NO_SETUP`. Critic v0 merges before
Slice 4 starts (§14 D2), so the dashboard **imports that classification** rather than
re-implementing it. `agent_runs` has no
`symbol` column, but `run_one_symbol` creates **one `scan_runs` row per symbol** per
invocation, so `signals ↔ agent_runs` is a plain join on `scan_id`. Consequently a "scan" in
the funnel and in "last scan per window" is an **invocation**, reconstructed by bucketing
`scan_runs.started_at` into the four schedule windows, not a `scan_runs` row.

### 5.2 Prerequisite pipeline changes (small, each its own step, before the page that needs them)

| Change | Needed by | Why |
|---|---|---|
| **Populate `agent_runs.latency_ms`, `token_usage`, `cost_usd`** in `run_scan._log_agent` from `StructuredCompletionResult` (today hard-coded `latency_ms=0`, cost NULL). | P2 (per-step cost), P7 (LLM cost/day). | The schema has the columns; the writer never filled them. |
| **New table `setup_evaluations`** — one row per Forecaster look at a setup: `id, setup_id FK→active_setups ON DELETE CASCADE, signal_id FK→signals, evaluated_at, status (STILL_VALID/AT_RISK/INVALIDATED/ERROR), outcome (nullable), reasoning, error (nullable), latency_ms, token_usage JSONB, cost_usd`, index `(setup_id, evaluated_at DESC)` and `(evaluated_at DESC)`. `Forecaster.run` inserts one row per setup **including an `ERROR` row** when the per-setup try/except catches. `active_setups.latest_evaluation` keeps working as the "last one wins" cache. | P1 health strip, P2 timeline, P7 coverage. | `agent_runs.scan_id` is `NOT NULL` → `scan_runs`, and the Forecaster sweep has no scan row and no signal link, so it cannot use `agent_runs` without bending that table. A dedicated table is the honest shape. Today failures are swallowed with no trace in the DB; this also closes the §7 observability action item. |
| *(optional)* Persist the Analyzer's detected zone set (OBs, FVGs, liquidity pools, swings) into `signals.features` or a side JSONB column. | Non-goal §4.3 if deferred. | Enables "what the Analyzer saw" chart overlays later. |

One new table (`setup_evaluations`, above) and two **read-optimised indexes** are added in
`schema.sql` (idempotent): `agent_runs (scan_id, agent_role)` and
`active_setups (status, opened_at DESC)`. Builder may add others driven by `EXPLAIN`.

### 5.2a Outcome ground truth for analytics (my call)

Two sources will exist: `signals.outcome` (the Forecaster's LLM verdict; `outcome_metadata`
holds only the `ForecasterUpdate`, never a realised R) and, once Critic v0 lands, the
Critic's `decision_resolutions` (deterministic price replay with `realized_r`, MAE/MFE, for
published, vetoed **and** gate-rejected decisions). Rule: **P6 uses `decision_resolutions`
when a row exists for the signal and falls back to `signals.outcome` otherwise**, and every
analytics chart carries a small source legend ("replayed" vs "forecaster-judged"). The equity
curve uses `realized_r` where present, else +RR / −1 / 0 by outcome. Counterfactual curves
for vetoed and gate-rejected decisions are shown only from `decision_resolutions`.

**Skeptic value definition:** an objection "counts" when `recommends_against` is true or
severity is HIGH. The Judge **respected** it if the ruling is SKIP, **overruled with caveat**
if PUBLISH_WITH_CAVEAT, **overruled** if PUBLISH. Three bars, not two; `NoMacroData` runs are
excluded and their count is shown beside the chart.

### 5.3 Risk-budget computation

Reuse `src/agents/orchestration/risk_gates.gather_risk_context` (it already reads open
setups and the 24 h window from the store) rather than re-implementing the counts. If its
signature needs a `now` and a store only, that is already the case; if it needs a snapshot,
split a store-only helper out of it (pipeline-side refactor, pure, tested).

## 6. API (Phase 1)

All routes under `/api`, JSON, Pydantic v2 response models in `src/dashboard/api/schemas.py`,
OpenAPI served at `/api/docs` (behind the authorizer). Pagination is `?limit=&cursor=`.

### 6.1 Endpoints

| Route | Page | Returns |
|---|---|---|
| `GET /api/health` (no auth) | deploy smoke test only | `{status:"ok", version}` — **must not touch the database or any AWS API**; an unauthenticated probe that woke Aurora would let any scanner keep it awake (~$25/month). No external uptime monitor consumes it. |
| `GET /api/system/db` (auth) | P7 | `{db: ok|resuming|error, latency_ms}` — the DB probe, behind the authorizer |
| `GET /api/overview` | P1 | health strip, risk budget, open setups, today's windows — one call to keep the landing page to a single round-trip |
| `GET /api/setups/open` | P1, P5 | OPEN setups with signal summary and latest evaluation |
| `GET /api/signals` | P3 | filtered page of journal rows with derived decision kind |
| `GET /api/signals/{id}` | P2 | full lifecycle: signal, all agent runs in order, Historian neighbours, Forecaster evaluations, setup row |
| `GET /api/signals/{id}/chart` | P2 | H4 candles ±N bars around `created_at` + overlay geometry |
| `GET /api/funnel?period=&group_by=` | P4 | funnel counts and rejections by rule |
| `GET /api/analytics/win-rate?by=tag|session|strategy|time` | P6 | FR-7.3 |
| `GET /api/analytics/skeptic-value` | P6 | FR-7.3 |
| `GET /api/analytics/equity-curve` | P6 | cumulative R series |
| `GET /api/critic/reports` , `/api/critic/reviews` | P6 | present only when the tables exist; otherwise 404 with a clear reason |
| `GET /api/markets` | P5 | watchlist with latest bias/phase per symbol and open-setup overlays |
| `GET /api/market/klines?symbol=&tf=H4&limit=` | P5, P2 | OHLCV via `BinanceProvider` |
| `GET /api/system/scans` , `/api/system/agents` , `/api/system/alarms` , `/api/system/metrics` , `/api/system/config` | P7 | as described in §4.2 P7 |

### 6.2 Module layout

```
src/dashboard/
  api/
    app.py            # FastAPI app factory + Mangum handler (`handler`)
    deps.py           # store / provider / boto3 clients (created once per container)
    schemas.py        # response models
    routers/          # one file per page group
    queries.py        # read-only SQL over DataApiSignalStore._execute (+ asyncpg twin for local tests)
    classify.py       # decision-kind stitching (reused from critic.inventory when merged)
dashboard-ui/         # Vite + React + TS
infrastructure/stacks/dashboard_stack.py
```

### 6.3 Reserved for Phase 2 (not built)

`POST /api/journal/{signal_id}`, `POST /api/controls/{pause|mute}`, tables
`execution_journal`, `system_flags`. Named here only so Phase 1 route and table names do
not collide.

**Phase 2 (separate design, starts after Step 4.10):**
1. **Trade journal first** — did the operator take the signal, fill price, size, real exit,
   notes; one table, two write routes, no pipeline change; analytics gain "system vs me".
2. **Operator controls second** — pause publishing, mute Telegram, trigger an ad-hoc scan,
   edit the watchlist; needs the scan Lambda to read `system_flags` on every run, so it
   changes live pipeline behaviour and gets its own review.

## 7. Telegram deep links

Every Telegram message that references a signal (new signal, caveat, veto note, Forecaster
update, closed setup) ends with the dashboard URL `https://<cloudfront>/signals/<id>`.

**No cross-stack reference in either direction** (anti-goal §4.4: a dashboard deploy must
never block or be blocked by a pipeline deploy, and Compute must stay recreatable as in the
2026-10 cutover). `DashboardStack` writes the CloudFront domain to a plain
`ssm.StringParameter` with the **static name** `/crypto-signals/dashboard-base-url`;
`ComputeStack` grants `ssm:GetParameter` on that static name (no import) and the notifier
reads it lazily once per container, falling back to "no link" when the parameter is absent.
In the other direction, the dashboard discovers the scan function and the four alarms **by
name prefix** (`CryptoSignals-Compute-ScanLambda`, `CryptoSignals-Monitoring-`) through
`cloudwatch:ListMetrics` / `DescribeAlarms`, never by imported ARN. Pipeline-side change,
one step, its own tests.

## 8. Security posture (deliberately minimal, per C1)

- Cognito user pool, one user, strong password policy, **no self-signup**, optional TOTP MFA
  left to the operator. Hosted UI callback restricted to the CloudFront domain.
- JWT authorizer on API Gateway validates issuer, audience/client, expiry. Lambda trusts the
  authorizer; it additionally rejects any request without the `requestContext.authorizer`
  claims (defence against direct Function/API misconfiguration).
- Lambda role: `rds-data` on the one cluster + its secret (`grant_data_api_access`),
  `cloudwatch:DescribeAlarms` + `cloudwatch:ListMetrics` + `cloudwatch:GetMetricData` (read),
  logs write. **No** SSM access, no `lambda:GetFunctionConfiguration` (the dashboard needs
  no third-party keys and must not read the scan function's config). No S3 access in
  Phase 1. Consequence for P7's config panel: it **infers** configuration from the database
  (watchlist from `scan_runs.symbols`, "macro providers configured" from whether recent
  Skeptic outputs are `NoMacroData`, schedule from the SPEC constants) and labels each value
  as inferred.
- S3 bucket private; CloudFront OAC; HTTPS only; HSTS; SPA fallback for 403/404 → `index.html`.
- All SQL is parameterised through the existing `_execute` helpers. No raw string formatting.
- cdk-nag runs on the new stack; suppressions must be justified in code as the other stacks do.

## 9. Cost

Operator usage assumption: ~20 dashboard sessions/week, ~10 minutes each, 60 s polling.

| Item | Monthly | Notes |
|---|---|---|
| Lambda (dashboard) | ~$0.00 | ~15 k invocations × 256 MB × ~0.5 s ≈ well inside the permanent free tier |
| API Gateway HTTP API | ~$0.00–0.02 | $1.00 per million requests after the first year |
| Cognito | $0.00 | free under 50 k MAU; advanced security off |
| S3 + CloudFront | ~$0.00–0.05 | KBs of assets; CloudFront free tier 1 TB/month |
| CloudWatch GetMetricData | ~$0.01 | $0.01 per 1 000 metrics requested; P7 only |
| Aurora | **+$0.20–0.60** | Dashboard use wakes Aurora (0.5 ACU min while awake, ~$0.07/ACU-h in ap-south-1) for the session plus the auto-pause window. This is the only line that scales with how much the operator browses. |
| **Total added** | **≈ $0.25–0.70** | vs. ≈ $12–15 for the SPEC's Fargate + ALB shape |

Guardrail: an AWS Budget alert at **$15/month total** on the account (one CDK resource,
free) — belongs to `MonitoringStack`; shipped in Step 4.0.

## 10. Build steps (replaces SPEC §4 Steps 4.1–4.11; each is one session)

| Step | Deliverable | Checkpoint |
|---|---|---|
| **pre** | Critic v0 merged to `main` (D2): operator applies the IAM grants, branch pushed, PR reviewed/merged, `critic.yml` backfill run. Not a Slice 4 step; a gate on starting one. | `critic.inventory.classify` importable from `main`; the three Critic tables present after a migrate invoke. |
| **4.0** | Pipeline prerequisites: derive `ScanSession` from the clock and record it (D1); populate `agent_runs` latency/tokens/cost; new `setup_evaluations` table + Forecaster writes per-setup rows incl. `ERROR`; two indexes; `resume_attempts` store parameter; AWS Budget alarm. | Unit tests incl. the session boundaries and a Cooldown force-skip; after deploy + a console `{"mode":"migrate"}` invoke, the next scan writes a real session and non-zero cost, and the next sweep writes evaluation rows. |
| **4.1** | `DashboardStack` scaffold: Lambda (FastAPI + Mangum, `/api/health` only), own layer via `lambda_assets.py` extras, HTTP API, log group, cdk-nag clean, stack added last in the deploy order (no SPA yet). | `GET /api/health` returns 200 from the API URL; synth test asserts resources. |
| **4.2** | Read-only queries + `classify.py` + overview/setups/signals endpoints (P1–P3 data). | Unit tests on SQL shape and parsing for the Data API backend; asyncpg integration test opt-in. |
| **4.3** | Funnel + analytics + system endpoints (P4, P6, P7 data) + klines proxy. | Same test pattern; `EXPLAIN` on the funnel query under 500 ms at 10 k signals (NFR-4.3). |
| **4.4** | Cognito user pool + JWT authorizer wired; all routes protected except `/api/health`. | Token-less call → 401; valid token → 200. |
| **4.5** | SPA scaffold: Vite/React/TS/Tailwind/TanStack Query/router, `config.json` loader, OIDC login, S3 + CloudFront + `BucketDeployment` (assets + `config.json`) in the stack, deploy workflow builds the SPA before `cdk deploy`, CI lints/type-checks. | Login → empty shell renders at the CloudFront URL on phone and laptop. |
| **4.6** | P1 Overview + P7 Health pages. | Red/green badges reflect a forced-failed scan in the DB. |
| **4.7** | P2 Signal detail + P3 Journal + Telegram deep links (pipeline-side env + formatter change). | A real signal's chain renders; Telegram message link opens it. |
| **4.8** | P4 Funnel + P6 Analytics. | Charts render against live data; empty states for sparse data. |
| **4.9** | P5 Live markets with charts and overlays. | Overlay lines match the open setup's numbers. |
| **4.10** | Hardening + docs: 503-on-resuming handling, error/empty states, responsive pass, `docs/dashboard.md`, SPEC §11 amendments, PROJECT_STATE update. | All universal checkpoints (SPEC §5.1); cost check after one week of use. |

## 11. SPEC amendments (ship with Step 4.1)

- §2.4: remove "Dashboard (Slice 4) is a separate long-running Fargate/App Runner service";
  the dashboard is a second zip-packaged Lambda behind API Gateway.
- §2.5 table: Backend API → "FastAPI on Lambda (Mangum) behind API Gateway HTTP API";
  Real-time delivery → "Client polling (60 s); no WebSocket"; add rows for Cognito JWT
  authorizer and `/api/*` behind CloudFront.
- FR-7.5: replace with "Updates shall be reflected within 60 s via client polling."
- NFR-3.5: "All inbound traffic terminates at CloudFront over HTTPS. WAF is not deployed
  for the single-user deployment; revisit if the dashboard gains more users."
- NFR-4.3: keep the < 500 ms query budget; add "except the first request after Aurora
  auto-pause, which may take up to 60 s and shall be surfaced to the user as a wake-up
  state, not an error."
- §4 Slice 4: replace Steps 4.1–4.11 with §10 above.
- CLAUDE.md §4 Compute row: same change as §2.4.

## 12. Left to the builder

- Exact SQL for each endpoint, index choices beyond §5.2, pagination cursor encoding.
- Lightweight-Charts overlay implementation (price lines vs. area series for the POI zone).
- Component library choices inside Tailwind (none required); icon set; dark/light mode
  (default: follow system).
- `lambda_assets.py` extras mechanism and the per-stack layer construct naming.
- Cursor encoding and the exact field trimming for the lifecycle payload (§3.4).
- Test scaffolding for the SPA (Vitest + Testing Library suggested).
- Cold-start tuning (memory 256–512 MB; measure).

## 14. Operator decisions (resolved 2026-10-19)

| # | Decision | Consequence |
|---|---|---|
| D1 | **Fix the session in Step 4.0.** `run_one_symbol` today hardcodes `session=AD_HOC` for every run, so rule 7 (no new signals in Asian 00:00–08:00 / Cooldown 21:00–00:00 UTC) never fires and the 22:03 "daily wrap" scan published inside Cooldown on 2026-10-11. Step 4.0 derives `ScanSession` from the UTC clock at scan start (London 08:00–13:00, NY 13:00–15:00, Overlap 15:00–21:00, Cooldown 21:00–00:00, Asian 00:00–08:00; exact boundaries per SPEC §1.6 / `risk_gates.check_session`) and records it on `scan_runs`. | **Live behaviour change:** the 22:03 scan still runs (Forecaster review + journal) but rule 7 now force-skips any new proposal in Cooldown. The Overview session badge, "win rate by session", and the Historian's session filter become real. Called out in the 4.0 PR and in PROJECT_STATE. |
| D2 | **Merge Critic v0 first.** The operator grants the deploy-role IAM permissions in `docs/operations.md §7.4`; `feat/slice-2-step-2.14-critic-v0` is pushed, reviewed and merged; then Slice 4 starts on top of it. | Step 4.0 builds on `src/agents/critic/inventory.py` (`DecisionKind`, `classify`) — no port in `classify.py`, which becomes a thin re-export. `decision_resolutions`, `decision_reviews`, `critic_reports` exist from day one, so the `/api/critic/*` endpoints and the §5.2a preference for replayed outcomes are unconditional. |
| D3 | **Read-only Phase 1 confirmed** (§4.1). | No write routes, no journal/flags tables, no pipeline flag reads in this design. |

## 13. Alternatives considered

| Alternative | Why not |
|---|---|
| SPEC's Fargate service + ALB + WebSocket | ≈ $12–15/month, pins Aurora awake, VPC placement; solves a real-time need the system does not have. |
| Lambda Function URL instead of API Gateway | Free, but no built-in Cognito JWT authorizer, so auth moves into Python; and CloudFront-to-Function-URL needs OAC + SigV4 tricks. HTTP API is effectively free at this volume and simpler. |
| AWS Amplify Hosting for the SPA | Fine, but another service and billing line; S3 + CloudFront is already in the SPEC and in CDK. |
| Streamlit / Grafana on a tiny instance | Always-on compute (cost), and Grafana cannot render the lifecycle timeline the way P2 needs. |
| Browser fetches Binance klines directly | Zero backend cost, but depends on Binance CORS policy and the operator's geolocation, and breaks the `DataProvider` rule. Kept as a possible later optimisation. |
| Build the execution journal / controls now | Deferred to Phase 2 (§4.1). |
