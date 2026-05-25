# Master Specification: Multi-Agent Crypto Trading Signal System

> **Document purpose:** This is the foundational context and step-by-step build guide for Claude Code. Every architectural decision, requirement, and implementation step has been pre-validated through extended research and stakeholder review. Claude Code should treat this document as the authoritative specification and consult it before making any independent design decisions.
>
> **Build philosophy:** Iterative, granular, vertical slices. Never attempt to build multiple components in one session. Always validate one step before proceeding to the next.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Agreed Tech Stack](#2-agreed-tech-stack)
3. [Comprehensive Requirements](#3-comprehensive-requirements)
4. [Step-by-Step Implementation Roadmap](#4-step-by-step-implementation-roadmap)
5. [Testing & Validation Checkpoints](#5-testing--validation-checkpoints)
6. [Additional Recommendations](#6-additional-recommendations)

---

## 1. System Overview

### 1.1 What We Are Building

A **multi-agent, autonomous crypto trading signal analyzer** that uses Smart Money Concepts (SMC) as its initial trading methodology, runs on a scheduled cadence, generates high-probability signals through adversarial multi-agent validation, delivers signals to the user via Telegram for manual execution, and continuously improves itself through empirical learning from its own performance history.

### 1.2 Core Operating Model

This is a **signal-only system** — it never places trades. The user receives Telegram alerts with full reasoning context and executes trades manually. The system has no broker credentials and no order-placement capability. This is a deliberate architectural constraint, not a temporary limitation.

### 1.3 The Multi-Agent Architecture

The system uses six specialized agent roles, all powered by the same Claude model with different prompts and different context scopes:

**Per-signal pipeline (runs on every scheduled scan):**
- **Analyzer** — Runs the SMC 5-layer protocol on raw market data and produces a structured trade proposal or skips
- **Historian** — Queries the journal of past signals to find similar setups and reports their empirical win rate
- **Skeptic** — Reads macro context (DXY, SPX, VIX) and on-chain data the Analyzer cannot see; tries to invalidate the proposal
- **Judge** — Weighs the proposal, historical evidence, and Skeptic objection; decides PUBLISH, PUBLISH_WITH_CAVEAT, or SKIP

**Background loops (run independently):**
- **Forecaster** — Every scan, re-evaluates all open hypotheses; sends update alerts when conditions materially change
- **Critic** — Weekly meta-review; reads the full reasoning chain of every signal; proposes rule changes to the strategy as pull requests

### 1.4 End-Goal Vision

This system must support, without architectural rewrites:

- **Multi-strategy operation** — SMC is strategy #1. Future strategies (funding-rate arbitrage, order-flow imbalance, sentiment-driven momentum, options-skew analysis) are added as new entry-point subgraphs that produce normalized signal proposals consumed by the same downstream agents.
- **Real-time dashboard** — A web interface displaying live system state, signal lifecycle traces, performance analytics, and strategy comparisons.
- **Multi-source data acquisition** — Today: Binance Futures + FRED + Twelve Data. Tomorrow: on-chain analytics (Dune, The Graph, Glassnode), news sentiment, options flow. New sources are additive.
- **Horizontal scaling** — As watchlist grows from 4 to 50+ symbols and concurrent active setups grow, no architectural component should become the bottleneck.

### 1.5 SMC Methodology Reference (Strategy #1)

The Analyzer implements a 5-layer protocol with a 5-gate filter. This is not negotiable — traditional indicators (RSI, MACD, Bollinger Bands, moving averages) are explicitly forbidden.

**The 5-Layer Protocol:**
- Layer 1: Data acquisition — multi-timeframe OHLCV (1D, 4H, 1H, 15m, 5m) + funding rate + OI
- Layer 2: HTF state machine — determine market phase (UPTREND/DOWNTREND/CONSOLIDATION), detect BOS/CHoCH, map BSL/SSL pools, calculate Premium/Discount zones with OTE (61.8%-78.6% Fibonacci)
- Layer 3: MTF POI validation — every Point of Interest must pass all 5 gates (Structure, Inefficiency, Liquidity Sweep, Derivatives Confluence, PO3 Temporal)
- Layer 4: LTF execution trigger — observe micro-sweep, verify CHoCH, confirm displacement, enter at LTF FVG + OTE confluence
- Layer 5: Risk parameters — SL beyond sweep wick, 1% max equity risk, minimum 1:3 R:R, move to breakeven at 1:1, partial at TP1

**Premium/Discount Constraint:** Never long in Premium zone, never short in Discount zone. Hard rule.

### 1.6 Risk Management Rules (Hard-Coded, Non-Negotiable)

These rules must be enforced programmatically — they cannot be overruled by any agent's reasoning:

1. Maximum 1% equity risk per signal
2. Minimum 1:3 Risk-to-Reward ratio
3. Premium/Discount enforcement (long only in Discount, short only in Premium)
4. Maximum 3 concurrent active signals
5. Maximum 5 signals per 24-hour period
6. Three consecutive losses triggers a mandatory 24-hour pause
7. No new signals during Asian session (00:00-08:00 UTC) or Cooldown (21:00-00:00 UTC)
8. Maximum 10x leverage recommendation
9. Correlated pair exposure checks (no stacking same-direction on BTC + ETH)
10. Funding rate cost calculation for hold duration

### 1.7 Scan Schedule

The system runs on UTC, 7 days per week (crypto never closes):

| Cron | Purpose |
|---|---|
| `3 8 * * *` | London open scan |
| `3 13 * * *` | NY open scan |
| `3 15 * * *` | London-NY overlap (highest confluence window) |
| `3 22 * * *` | Daily wrap and active setups review |
| `0 21 * * 0` | Weekly Critic meta-review |

Minutes set to `:03` deliberately to avoid clock jitter on `:00` minutes.

---

## 2. Agreed Tech Stack

### 2.1 Application Layer

| Component | Technology | Version | Rationale |
|---|---|---|---|
| Language | Python | 3.11+ | LangGraph + Pydantic v2 + Anthropic SDK all require modern Python |
| LLM Provider | Anthropic SDK | Latest stable | Direct SDK calls; no framework wrapper |
| Model | Claude Sonnet 4.5 | — | Best balance of reasoning quality and cost for agent roles |
| Agent Orchestration | LangGraph | v1.0+ | Stateful, cyclic, checkpointable; supports multi-strategy subgraphs |
| Data Validation | Pydantic | v2 | Typed I/O at every agent boundary; non-negotiable |
| Observability | Langfuse (self-hosted) | Latest | Production debugging for cyclic graphs is non-negotiable |
| Async runtime | asyncio | stdlib | Native Anthropic SDK supports async |

### 2.2 Data Layer

| Component | Technology | Rationale |
|---|---|---|
| Primary database | RDS Serverless v2 PostgreSQL 16+ | Single query language for everything; scales to zero when idle |
| Vector extension | pgvector | Native vector similarity inside Postgres; no separate vector DB |
| Blob storage | Amazon S3 (versioning enabled) | Audit retention, large reasoning logs, cold data |
| Secrets | AWS Secrets Manager | Anthropic API key, Telegram bot token, FRED/Twelve Data keys |

### 2.3 Data Acquisition Layer

| Source | Library/API | Purpose |
|---|---|---|
| Binance Futures | CCXT (preferred) or python-binance | Klines, funding rate, OI, mark price |
| Macro fundamentals | FRED API (free, government-grade) | DXY, 10Y yield, Fed funds |
| Intraday indices | Twelve Data free tier | SPX, VIX intraday |
| Future on-chain | Dune Analytics / The Graph / Glassnode free tier | Added later via provider interface |

All data sources sit behind a uniform `DataProvider` interface. Agents never call libraries directly.

### 2.4 Compute Layer

| Component | Technology | Rationale |
|---|---|---|
| Container compute | ECS Fargate | Same paradigm for scheduled tasks and long-running services; no Lambda 15-min cliff |
| Scheduling | EventBridge Scheduler | Triggers Fargate scheduled tasks for cron scans |
| Container registry | Amazon ECR | Standard AWS container store |
| Service discovery | AWS Cloud Map | For internal service-to-service calls between dashboard backend and agents |

### 2.5 Dashboard Layer (Built in Slice 4)

| Component | Technology | Rationale |
|---|---|---|
| Backend API | FastAPI | Same Python stack as agents; runs in same Fargate cluster |
| Real-time delivery | WebSocket + Postgres LISTEN/NOTIFY | Push state changes to dashboard |
| Frontend | React + TypeScript | Standard SPA stack |
| Charts | TradingView Lightweight Charts | Industry standard for crypto; free; documented |
| Frontend hosting | S3 + CloudFront | Standard static SPA deployment |

### 2.6 Infrastructure & DevOps

| Component | Technology | Rationale |
|---|---|---|
| IaC | AWS CDK in Python | Multi-stack structure; matches AWS surface area we need |
| Claude Code plugin | `aws-cdk@aws-skills` | Reduces CDK generation errors; MCP-integrated AWS docs |
| Security gates | cdk-nag | Catches IAM/networking issues before deploy |
| Local dev | LocalStack + docker-compose | Tight Claude Code iteration loop despite production complexity |
| Version control | GitHub | Standard |
| CI/CD | GitHub Actions | Tag-based production deploys |
| Container scanning | Trivy (in CI) | Catches base image vulnerabilities |

### 2.7 Notifications

| Component | Technology | Rationale |
|---|---|---|
| Signal delivery | Telegram Bot API via httpx | Single dependency; no library churn risk |
| Future channels | Plugged in behind a `Notifier` interface | Easy to add Discord, email, etc. |

### 2.8 Code Quality

| Tool | Purpose |
|---|---|
| `ruff` | Linting and formatting |
| `mypy` | Static type checking |
| `pytest` + `pytest-asyncio` | Testing |
| `pre-commit` | Run quality gates on commit |

---

## 3. Comprehensive Requirements

### 3.1 Functional Requirements

#### 3.1.1 Multi-Agent Pipeline

**FR-1.1** The system shall execute the Analyzer → Historian → Skeptic → Judge pipeline on every scheduled scan.

**FR-1.2** The Analyzer shall implement the SMC 5-layer protocol with the 5-gate filter and produce one of: (a) a structured `SignalProposal` Pydantic model, or (b) a `SkipDecision` with reasoning. No other outputs are valid.

**FR-1.3** The Analyzer shall enforce all hard rules from Section 1.6 before producing any proposal. A proposal that violates any hard rule must be a skip, with the violating rule logged.

**FR-1.4** The Historian shall query the signal journal using a three-stage retrieval: hard categorical filters → tag overlap ranking → numeric L2 distance ranking. It returns the top-K (default K=10) most similar past setups with their outcomes.

**FR-1.5** The Skeptic shall fetch macro context independently from the Analyzer's data sources. It shall produce a strongest-objection report with a severity rating (low/medium/high) and concrete reasoning citing specific macro data points.

**FR-1.6** The Judge shall consume the proposal, historian retrieval, and Skeptic objection, and produce one of: PUBLISH, PUBLISH_WITH_CAVEAT, or SKIP. Every decision must include written reasoning that is appended to the journal.

**FR-1.7** The full reasoning chain (proposal + retrieval + objection + judgment) must be persisted to the journal regardless of outcome — including skipped signals.

#### 3.1.2 Background Loops

**FR-2.1** The Forecaster shall run at every scheduled scan and re-evaluate every signal in the `active_setups` table. For each setup, it produces one of three outcomes: STILL_VALID (no action), AT_RISK (send Telegram update), or INVALIDATED (close setup with outcome logged).

**FR-2.2** The Critic shall run weekly (Sunday 21:00 UTC) and analyze the full week's journal. It produces a markdown report identifying patterns in decision-making errors and proposes specific rule changes. The Critic shall not modify rules directly — it shall open a GitHub Pull Request against a `claude/proposed-rules` branch for human review.

**FR-2.3** The Critic shall use pgvector semantic similarity over confluence narratives to identify market-regime patterns that the structured tag vocabulary does not capture.

#### 3.1.3 Multi-Strategy Support

**FR-3.1** Each trading strategy shall be implemented as an independent LangGraph subgraph that produces a normalized `SignalProposal` Pydantic model.

**FR-3.2** Adding a new strategy must not require modification to the Historian, Skeptic, Judge, Forecaster, or Critic. The orchestration core treats strategies polymorphically.

**FR-3.3** The strategy registry shall be configuration-driven (a `strategies.yaml` or similar). Enabling/disabling a strategy must not require code changes.

#### 3.1.4 Data Acquisition

**FR-4.1** All external data sources shall be accessed through a `DataProvider` interface with normalized Pydantic return types. Agents shall never call provider libraries directly.

**FR-4.2** Each provider implementation shall handle its own retries, rate limiting, and failure modes. On unrecoverable failure, providers shall raise typed exceptions that propagate to the agent with explicit "data unavailable" semantics.

**FR-4.3** The Skeptic shall degrade gracefully — if macro data is unavailable, it produces a `NoMacroData` result that the Judge interprets as "downgrade confidence to medium" rather than treating absence as no-objection.

#### 3.1.5 Notifications

**FR-5.1** Signal alerts shall be delivered to Telegram via a `Notifier` interface. The implementation shall use direct httpx calls (no library wrappers).

**FR-5.2** The Telegram alert format shall include: symbol, direction, entry zone, invalidation, targets, R:R, Historian win-rate statistic, Skeptic objection (if any), and a "Signal only — manual execution required" footer.

**FR-5.3** Forecaster updates shall be distinguishable from new signals (different prefix, different formatting).

#### 3.1.6 Persistence

**FR-6.1** All system state shall live in Postgres. The schema must include tables for: `signals`, `active_setups`, `reasoning_chains`, `provider_snapshots`, `strategy_configs`, `agent_runs`, `proposed_rules`.

**FR-6.2** Every agent execution shall be logged to `agent_runs` with: timestamp, agent role, strategy (if applicable), input hash, output, latency, token usage, cost.

**FR-6.3** Postgres shall be the single source of truth. S3 is used only for: large reasoning blob retention (>1MB), raw kline snapshots at scan time (for audit), and CloudFront-hosted dashboard assets.

### 3.2 Dashboard Requirements (Implemented in Slice 4)

**FR-7.1** The dashboard shall provide a **Live State** view showing current bias on each watched symbol, all active setups with current PnL trajectory, latest funding/OI snapshot.

**FR-7.2** The dashboard shall provide a **Signal Lifecycle** view that traces any signal through Analyzer → Historian → Skeptic → Judge → Forecaster → outcome with full reasoning at each step.

**FR-7.3** The dashboard shall provide a **Performance Analytics** view showing: win rate by tag, by session, by strategy; Skeptic value analysis (win rate when overruled vs respected); Critic-proposed rule changes status.

**FR-7.4** The dashboard backend (FastAPI) shall read directly from Postgres. It shall NOT invoke the agent pipeline. Read and write paths are fully decoupled.

**FR-7.5** Real-time updates shall use Postgres LISTEN/NOTIFY → FastAPI WebSocket → React frontend.

### 3.3 Non-Functional Requirements

#### 3.3.1 Reliability

**NFR-1.1** A single scan failure shall not affect subsequent scans. Each scan is an independent execution unit.

**NFR-1.2** Provider failures shall not crash the system. Partial data is acceptable; agents must handle missing inputs gracefully.

**NFR-1.3** LangGraph state shall be checkpointed to Postgres. A crashed scan must be resumable from its last checkpoint.

**NFR-1.4** All agent LLM calls shall have a structured output validation step. Malformed outputs trigger up to 3 retries with exponential backoff before failing the agent.

#### 3.3.2 Observability

**NFR-2.1** Every agent run shall produce a Langfuse trace. The trace must include all LLM calls, all tool calls, all state transitions, and final outputs.

**NFR-2.2** CloudWatch alarms shall fire on: scan failure rate > 10% over 24h, provider error rate > 20% over 1h, agent latency P95 > 2 minutes, Postgres CPU > 80%.

**NFR-2.3** Cost tracking shall be implemented at the agent level: every LLM call logged with token usage and computed dollar cost. Weekly cost report generated by the Critic.

#### 3.3.3 Security

**NFR-3.1** All secrets (Anthropic API key, Telegram bot token, FRED/Twelve Data keys, database credentials) shall live in AWS Secrets Manager. No secret shall be committed to the repository.

**NFR-3.2** The IAM role for the Fargate task shall follow least-privilege: read-only access to Secrets Manager for required secrets only, read-write to specific S3 prefixes only, no broader AWS access.

**NFR-3.3** Postgres shall run in a private subnet with no public IP. Access only from within the VPC.

**NFR-3.4** Dashboard authentication shall be implemented via AWS Cognito (single-user initially, extensible to multi-user later).

**NFR-3.5** All inbound HTTPS traffic to the dashboard shall terminate at CloudFront with AWS WAF rules for basic protection.

#### 3.3.4 Performance

**NFR-4.1** A complete scan (Analyzer → Historian → Skeptic → Judge for a single symbol) shall complete within 5 minutes. Multi-symbol watchlists run symbols in parallel.

**NFR-4.2** Forecaster evaluation of a single active setup shall complete within 30 seconds.

**NFR-4.3** Dashboard API responses shall be < 500ms for queries; WebSocket state updates shall be delivered within 2 seconds of the underlying state change.

#### 3.3.5 Cost

**NFR-5.1** Target steady-state AWS infrastructure cost: < $100/month at 4 symbols, 4 scans/day. (RDS Serverless idle + Fargate task time + S3 + CloudFront + Secrets Manager + EventBridge.)

**NFR-5.2** Target LLM cost: tracked per agent per scan. Budget alarm at $200/month spending.

#### 3.3.6 Maintainability

**NFR-6.1** All code shall pass `ruff check`, `ruff format --check`, and `mypy --strict`. Pre-commit hooks shall enforce this locally.

**NFR-6.2** Test coverage shall be ≥ 80% for non-LLM code (provider implementations, schemas, persistence layer, utility functions). LLM-calling code is tested via integration tests with mocked LLM responses.

**NFR-6.3** All Pydantic models shall be documented with field descriptions that double as documentation and as LLM-readable schema hints.

---

## 4. Step-by-Step Implementation Roadmap

> **Critical guidance for Claude Code:** Build one step at a time. Validate completion using the testing checkpoints in Section 5 before moving to the next step. Do not batch multiple steps into a single session — context window overload causes silent quality degradation.
>
> **Each step is sized to be completable in one focused Claude Code session (typically 30-60 minutes of agent time).**

The build is organized into **four vertical slices**. Each slice goes through the entire stack (data, agents, storage, infrastructure). No "build all the storage first" phases — each slice is end-to-end functional on the production stack.

---

### Slice 1: End-to-End Substrate Validation (Weeks 1-3)

**Slice goal:** Prove the production stack works end-to-end with one symbol, one strategy, one agent, manual triggering. Output a Telegram message from a Fargate-deployed container reading from a real Postgres database.

#### Step 1.1: Repository scaffolding

- Create GitHub repository `crypto-signals-system`
- Add `.gitignore` for Python, AWS CDK, IDE files
- Create directory structure:
  ```
  src/
    agents/
    providers/
    persistence/
    notifications/
    config/
    common/
  infrastructure/
    stacks/
  tests/
    unit/
    integration/
  scripts/
  docs/
  ```
- Add `README.md` placeholder
- Add `pyproject.toml` with project metadata
- Initial commit to `main`

#### Step 1.2: Python tooling setup

- Add `pyproject.toml` dependencies (minimal: pydantic, anthropic, langgraph, httpx, asyncpg)
- Add dev dependencies (pytest, pytest-asyncio, ruff, mypy, pre-commit)
- Configure `ruff` in `pyproject.toml` with strict settings
- Configure `mypy --strict` in `pyproject.toml`
- Add `.pre-commit-config.yaml` with ruff, mypy, basic file hygiene
- Run `pre-commit install`

#### Step 1.3: Core Pydantic models

- Create `src/common/models/__init__.py`
- Define `SignalProposal` Pydantic model with all required fields per spec
- Define `SkipDecision` Pydantic model
- Define `JudgeRuling` enum (PUBLISH, PUBLISH_WITH_CAVEAT, SKIP)
- Define `ScanContext` model containing run metadata
- Add docstrings on every field (these serve as LLM schema hints)
- Add unit tests verifying model validation (rejects invalid R:R, rejects out-of-range tags, etc.)

#### Step 1.4: Data provider interface

- Create `src/providers/base.py` with `DataProvider` abstract base class
- Define `MarketSnapshot` and `MacroContext` Pydantic return types
- Implement `BinanceProvider` using CCXT for one method only: fetch 4H klines for one symbol
- Add unit tests with mocked CCXT responses
- Add one integration test (skip in CI, run locally) that actually calls Binance

#### Step 1.5: Minimal SMC analyzer (no LangGraph yet)

- Create `src/agents/analyzer/smc_analyzer.py`
- Port the existing `detect_structure.py` logic into a Python module (no CLI, no print statements)
- Implement a function `analyze(snapshot: MarketSnapshot) -> SignalProposal | SkipDecision`
- For Slice 1, implement only HTF bias detection — always returns SKIP unless 4H bias is clear
- Add unit tests with synthetic kline data covering bullish, bearish, and ranging cases

#### Step 1.6: Anthropic SDK integration with structured outputs

- Create `src/common/llm.py` with a wrapper function that calls Anthropic SDK
- Implement structured output via Pydantic: pass the schema, parse the response, validate, retry up to 3x on validation failure
- Add a `dry_run` mode that returns a fixture response for testing
- Add unit tests with mocked Anthropic responses (success, validation failure, retry)

#### Step 1.7: LangGraph agent shell

- Create `src/agents/orchestration/graph.py`
- Define a minimal `AgentState` TypedDict with fields: `scan_context`, `proposal`, `decision`
- Add one node: `analyzer_node` that calls the SMC analyzer
- Add an edge from START to `analyzer_node` to END
- Add unit test that runs the graph end-to-end with mocked data

#### Step 1.8: Postgres schema with pgvector

- Add `pgvector` to dependencies (`psycopg`, `pgvector-python`)
- Create `src/persistence/schema.sql` with initial tables:
  - `signals` (id, scan_id, symbol, strategy, direction, status, created_at, payload JSONB)
  - `agent_runs` (id, scan_id, agent_role, input_hash, output JSONB, latency_ms, token_usage JSONB, created_at)
  - `scan_runs` (id, started_at, completed_at, status, error_message)
- Create migration runner script in `scripts/migrate.py`
- Run locally against Docker Postgres for testing

#### Step 1.9: Persistence layer

- Create `src/persistence/repositories.py`
- Implement `SignalRepository` with methods: `create_signal`, `get_by_id`, `list_recent`
- Implement `AgentRunRepository` with methods: `log_run`
- Implement `ScanRunRepository` with `start_scan`, `complete_scan`, `fail_scan`
- Use asyncpg, all methods async
- Add integration tests against Docker Postgres

#### Step 1.10: Telegram notifier

- Create `src/notifications/base.py` with `Notifier` abstract base class
- Implement `TelegramNotifier` using httpx (no library)
- Implement message formatting for signals
- Add unit tests with mocked httpx
- Add a manual test script to actually send a message

#### Step 1.11: Configuration management

- Create `src/config/settings.py` using Pydantic Settings
- Define all configuration: database URL, Anthropic API key, Telegram bot token, chat ID, scan symbols, log level
- Load from environment variables (which will be from Secrets Manager in production, .env locally)
- Add `.env.example` with all required variables (no values)
- Add `.env` to `.gitignore`

#### Step 1.12: Local end-to-end runner

- Create `scripts/run_scan.py` that:
  - Loads config
  - Initializes Postgres connection
  - Fetches data for one symbol from Binance
  - Runs the LangGraph (Analyzer only)
  - Persists results
  - Sends Telegram message if proposal exists
- Run this locally end-to-end with a real Anthropic API call and a real Telegram message arriving on your phone

#### Step 1.13: Docker containerization

- Create `Dockerfile` based on `python:3.11-slim`
- Multi-stage build: install dependencies, copy code, set entrypoint
- Create `docker-compose.yml` with: app container, Postgres with pgvector, LocalStack for AWS services
- Verify `docker compose up` runs the full system locally
- Add `.dockerignore`

#### Step 1.14: CDK project initialization

- Initialize CDK project in `infrastructure/` (`cdk init app --language python`)
- Install `aws-cdk@aws-skills` Claude Code plugin per AWS documentation
- Install `cdk-nag` for security checks
- Create empty stacks: `NetworkStack`, `DataStack`, `ComputeStack`, `SchedulingStack`, `MonitoringStack`
- Verify `cdk synth` produces valid CloudFormation

#### Step 1.15: NetworkStack implementation

- Define VPC with public, private (egress), and isolated subnets across 2 AZs
- Define VPC endpoints for: S3, ECR, Secrets Manager, CloudWatch Logs (avoid NAT Gateway costs)
- Apply cdk-nag rules
- Deploy to dev account
- Verify VPC exists, subnets created, endpoints reachable

#### Step 1.16: DataStack implementation

- Define RDS Serverless v2 PostgreSQL 16 cluster in isolated subnets
- Define S3 bucket with versioning, lifecycle policy (Glacier after 90 days)
- Define Secrets Manager entries (placeholder values; populate manually after deploy)
- Output: DB connection details, S3 bucket name
- Deploy and verify

#### Step 1.17: ComputeStack implementation

- Define ECR repository
- Define ECS Fargate cluster
- Define Fargate task definition with: container image, IAM role with least-privilege, log group, secrets injected from Secrets Manager
- Output: cluster name, task definition ARN
- Deploy and verify

#### Step 1.18: SchedulingStack implementation

- Define EventBridge Scheduler with one rule for now: `3 8 * * *` UTC
- Define Fargate task target with appropriate IAM permissions
- Deploy and verify rule appears in EventBridge console

#### Step 1.19: GitHub Actions CI

- Create `.github/workflows/ci.yml`
- On every push and PR: ruff check, mypy strict, pytest with coverage report
- On every push to main: run full test suite, build Docker image, scan with Trivy
- Verify CI passes

#### Step 1.20: GitHub Actions CD

- Create `.github/workflows/deploy-dev.yml` — triggered on push to `main` after CI passes
  - Build Docker image
  - Push to ECR (dev account)
  - Run `cdk deploy` for dev stack
- Create `.github/workflows/deploy-prod.yml` — triggered on git tag push
  - Same flow but to production account
  - Add manual approval gate
- Verify a push to main deploys to dev, and the deployed task runs successfully when manually triggered

#### Step 1.21: First production scan

- Manually trigger the deployed Fargate task in dev account
- Verify: Postgres receives signal record, Telegram receives message, Langfuse trace is captured, CloudWatch logs are clean
- This is the "Hello World" of the production stack — celebrate this milestone

---

### Slice 2: Full Per-Signal Pipeline + Forecaster (Weeks 4-7)

**Slice goal:** Extend Slice 1 to the full four-agent pipeline plus the Forecaster background loop. By end of slice, every scan produces high-quality reasoned signals (or skips), the Forecaster updates active setups, and the journal has structure for the Historian to use.

#### Step 2.1: Expand SMC analyzer

- Port the rest of the SMC scripts (`detect_fvg.py`, `detect_ob.py`, `detect_liquidity.py`, `derivatives_data.py`) as Python modules
- Combine into a `full_smc_analysis` function that produces a complete `SignalProposal` when all gates pass
- Add unit tests for each detector with synthetic data

#### Step 2.2: Multi-timeframe data provider

- Extend `BinanceProvider` to fetch multiple timeframes in one call
- Add funding rate and OI methods
- Add rate limiting (token bucket) to respect Binance's 2400 weight/minute
- Add integration tests

#### Step 2.3: Macro data providers

- Create `FREDProvider` for DXY, 10Y yield, Fed funds
- Create `TwelveDataProvider` for SPX, VIX intraday
- Each provider behind the `DataProvider` interface with normalized `MacroContext` returns
- Implement graceful degradation on failure (return `NoMacroData` sentinel)
- Add tests

#### Step 2.4: Historian implementation

- Add tables to schema: extend `signals` table with `tags` (text array), `features` (JSONB), `outcome` (enum), `outcome_metadata` (JSONB)
- Implement `HistorianRepository` with three-stage retrieval:
  - Stage 1: SQL hard filters (direction, session, primary_poi_type)
  - Stage 2: Tag overlap ranking using PostgreSQL array operators
  - Stage 3: Numeric L2 distance ranking using a SQL function
- Implement `historian_node` for LangGraph that calls the repository and produces a `HistorianReport` Pydantic model
- Add unit tests with synthetic journal data
- Add fixture script to seed 50 synthetic signals for development

#### Step 2.5: Skeptic implementation

- Create `src/agents/skeptic/skeptic.py`
- Implement `skeptic_node` for LangGraph that:
  - Fetches macro via FREDProvider and TwelveDataProvider in parallel
  - Constructs a Skeptic prompt with proposal + macro context
  - Calls Anthropic SDK with `SkepticObjection` output schema
  - Returns objection with severity rating
- Handle macro data unavailable case
- Add tests with mocked LLM responses

#### Step 2.6: Judge implementation

- Create `src/agents/judge/judge.py`
- Implement `judge_node` for LangGraph that:
  - Consumes proposal, historian report, skeptic objection
  - Calls Anthropic SDK with `JudgeRuling` output schema
  - Returns ruling with written reasoning
- Add tests covering: strong proposal + supportive history + weak objection = PUBLISH
- Add tests covering: strong proposal + weak history + strong objection = SKIP
- Add tests covering: borderline cases = PUBLISH_WITH_CAVEAT

#### Step 2.7: Wire the full pipeline in LangGraph

- Update `graph.py` to connect: analyzer → historian → skeptic → judge → notify_or_skip
- Add conditional edge: if Analyzer returns SkipDecision, skip remaining agents
- Add Langfuse tracing to every node
- Add LangGraph Postgres checkpointer
- Add integration test that runs the full graph with all agents mocked

#### Step 2.8: Active setups tracking

- Add table: `active_setups` (id, signal_id, opened_at, status, last_evaluated_at, latest_evaluation JSONB)
- Implement `ActiveSetupRepository`
- On Judge PUBLISH or PUBLISH_WITH_CAVEAT, insert into `active_setups`
- Implement closing logic (called by Forecaster)

#### Step 2.9: Forecaster implementation

- Create `src/agents/forecaster/forecaster.py`
- Implement standalone function that:
  - Reads all active setups from Postgres
  - For each: refetches market data, evaluates against invalidation/target/regime change
  - Calls Anthropic SDK with `ForecasterUpdate` output schema
  - Outputs STILL_VALID, AT_RISK, or INVALIDATED
- On AT_RISK: send Telegram update via Notifier
- On INVALIDATED: close setup with outcome, log to journal
- Add tests

#### Step 2.10: Forecaster scheduling

- Update `SchedulingStack` to add Forecaster invocations
- The Forecaster runs after every scan (chained via Step Functions or as a separate scheduled task)
- Decision: separate scheduled task to maintain clean separation
- Add cron `5 8,13,15,22 * * *` for Forecaster (2 minutes after each scan)
- Deploy and verify

#### Step 2.11: Risk management enforcement

- Create `src/agents/orchestration/risk_gates.py`
- Implement hard-rule checks as pure functions that return `RiskCheckResult`
- Insert into pipeline between analyzer output and historian: if risk check fails, force SKIP
- Add tests for every hard rule

#### Step 2.12: Production secrets and operations

- Populate Secrets Manager with real values: Anthropic API key, Telegram bot token, FRED API key, Twelve Data API key
- Add CloudWatch alarms per NFR-2.2
- Test alarm firing with a deliberate failure
- Document the operations runbook in `docs/operations.md`

#### Step 2.13: Multi-symbol parallelization

- Update scan runner to process watchlist in parallel using asyncio
- Add per-symbol error isolation (one symbol failing doesn't crash the scan)
- Test with the full watchlist: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT

#### Step 2.14: Full pipeline production validation

- Let the system run autonomously for 1 week
- Review every signal manually
- Tune prompts as needed
- Verify journal accumulation, Forecaster updates, Telegram delivery all work

---

### Slice 3: Multi-Strategy Architecture + Critic + Embeddings (Weeks 8-11)

**Slice goal:** Refactor the Analyzer into a strategy registry. Add the Critic for weekly meta-review with embeddings-based pattern discovery. By end of slice, the system is multi-strategy-capable and self-improving.

#### Step 3.1: Strategy abstraction

- Create `src/agents/strategies/base.py` with `Strategy` abstract base class
- Define interface: `analyze(scan_context: ScanContext) -> SignalProposal | SkipDecision`
- Refactor SMC analyzer to implement this interface as `SMCStrategy`
- Update LangGraph analyzer node to dispatch to the registered strategy

#### Step 3.2: Strategy registry

- Create `src/config/strategies.yaml`
- Define strategy configuration: name, class, enabled, watchlist, schedule overrides
- Implement `StrategyRegistry` that loads from YAML and provides lookup
- Update orchestration to iterate registered strategies on each scan
- Add unit tests for registry loading

#### Step 3.3: Second strategy stub

- Create `src/agents/strategies/funding_rate_strategy.py` as a placeholder
- Implement a trivial strategy: long when funding < -0.05%, short when > +0.05%
- Add to registry as disabled by default
- Verify the registry can host multiple strategies even if only SMC is active

#### Step 3.4: Embedding generation

- Add embedding generation step at the end of Judge: embed the `confluence_narrative` field
- Use OpenAI `text-embedding-3-small` (cheap, sufficient for Critic discovery)
- Add embedding column to `signals` table (`narrative_embedding vector(1536)`)
- Add pgvector HNSW index for efficient similarity search
- Backfill embeddings for existing journal entries

#### Step 3.5: Critic implementation

- Create `src/agents/critic/critic.py`
- Implement weekly run that:
  - Reads all signals from the past 7 days (with reasoning chains)
  - Groups by outcome (W, L, BE)
  - Uses pgvector to find clusters in confluence narratives among losses
  - Computes win-rate breakdowns by tag, session, strategy
  - Calls Anthropic with `CriticReport` output schema
  - Identifies patterns of decision-making error
- Output: markdown report + list of `ProposedRuleChange` items

#### Step 3.6: Critic PR opening

- Implement GitHub PR creation using GitHub API
- Critic writes proposed changes to `claude/proposed-rules-YYYY-MM-DD` branch
- Opens PR against `main` with the markdown report as PR description
- Sends Telegram notification: "Weekly Critic report ready for review: [PR link]"
- Add IAM permissions for GitHub token access via Secrets Manager

#### Step 3.7: Critic scheduling

- Add Sunday 21:00 UTC schedule to `SchedulingStack`
- Deploy and verify
- Manually trigger first Critic run; review the PR

#### Step 3.8: Self-improvement validation

- Let the system run autonomously for 4 weeks
- Review and selectively merge Critic-proposed rule changes
- Track: did accepted rule changes improve subsequent win rate?
- Document lessons in `docs/critic-learnings.md`

---

### Slice 4: Real-Time Dashboard (Weeks 12-15)

**Slice goal:** Build the FastAPI dashboard backend and React frontend. By end of slice, the user can watch the system in real time, trace signal lifecycles, and view performance analytics.

#### Step 4.1: FastAPI service scaffolding

- Create `src/dashboard/api/` directory
- Initialize FastAPI app with health check endpoint
- Add to existing Docker image with new entry point
- Update CDK `ComputeStack`: add Fargate service definition (not scheduled task) for the dashboard
- Add internal Application Load Balancer
- Deploy and verify the health check responds

#### Step 4.2: Read-only repository extensions

- Create `src/dashboard/api/repositories.py` with read-only methods optimized for dashboard queries
- Implement: `list_active_setups`, `get_signal_lifecycle`, `get_performance_breakdown`
- Add database indexes per query patterns
- Add tests

#### Step 4.3: REST endpoints

- Implement `/api/active-setups` — list current open setups with latest evaluation
- Implement `/api/signals/{id}/lifecycle` — full reasoning chain trace
- Implement `/api/analytics/win-rate` — breakdowns by tag, session, strategy
- Implement `/api/analytics/skeptic-value` — comparison of overruled vs respected
- Add OpenAPI documentation
- Add integration tests

#### Step 4.4: WebSocket real-time updates

- Add Postgres LISTEN/NOTIFY triggers on `signals`, `active_setups`, `agent_runs`
- Implement FastAPI WebSocket endpoint `/ws/state`
- Subscribe to Postgres NOTIFY channels in WebSocket handler
- Push updates to connected clients
- Add tests with WebSocket test client

#### Step 4.5: Cognito authentication

- Add AWS Cognito User Pool to CDK
- Implement JWT verification middleware in FastAPI
- Create single user manually in Cognito console
- Protect all `/api/*` and `/ws/*` endpoints with auth
- Test with valid and invalid tokens

#### Step 4.6: React frontend scaffolding

- Initialize React + TypeScript project in `dashboard-ui/`
- Install: TradingView Lightweight Charts, react-router, TanStack Query, Tailwind CSS
- Set up Cognito auth flow on frontend
- Set up build pipeline
- Deploy build artifacts to S3 + CloudFront via CDK

#### Step 4.7: Live State view

- Implement page: grid of watched symbols with current bias indicator
- Embedded TradingView chart for each symbol
- Overlay current active setup zones
- Connect to WebSocket for live updates

#### Step 4.8: Signal Lifecycle view

- Implement page: search and select a signal by ID or recent
- Render the full reasoning chain as a vertical timeline
- For each agent step: show input, output, latency, token usage
- Include direct link to Langfuse trace

#### Step 4.9: Performance Analytics view

- Implement page with multiple charts:
  - Win rate over time (line chart)
  - Win rate by tag (bar chart)
  - Win rate by session (bar chart)
  - Skeptic value analysis (when overruled vs respected — bar chart)
  - Critic-proposed rule changes status (table)
- Use Recharts or similar

#### Step 4.10: WAF and CloudFront hardening

- Add AWS WAF rules to CloudFront: rate limiting, geo restriction, basic SQL injection protection
- Configure HTTPS-only with HSTS
- Test from a non-allowed region (should be blocked)

#### Step 4.11: End-to-end dashboard validation

- Verify a new signal appearing in Postgres triggers WebSocket update within 2 seconds
- Verify all three views render correctly with real data
- Verify Cognito auth flow works on mobile
- Document the dashboard in `docs/dashboard.md`

---

### Future Slices (Not Built Now, Documented for Context)

These are out of scope for the current build but should not require rework of Slices 1-4:

- **Slice 5: Second real strategy** (funding rate arbitrage, fully implemented)
- **Slice 6: On-chain data sources** (Dune Analytics, Glassnode)
- **Slice 7: Multi-user dashboard** with role-based access
- **Slice 8: Approach 2 graduation** — optional auto-execution with explicit user approval per trade

---

## 5. Testing & Validation Checkpoints

> **Discipline:** Before moving to the next step, the current step must satisfy ALL applicable checkpoints. If a checkpoint fails, fix the issue before continuing.

### 5.1 Universal Checkpoints (Apply to Every Step)

- `ruff check .` passes with zero warnings
- `ruff format --check .` passes
- `mypy --strict src/` passes
- `pytest` passes with no skipped tests (unless explicitly marked integration)
- Pre-commit hooks pass on commit
- No secrets committed (verified by `git diff` review)

### 5.2 Step-Type-Specific Checkpoints

**For Pydantic model steps:**
- Models reject all invalid inputs in unit tests
- Models accept all valid inputs in unit tests
- Field docstrings are present and informative

**For provider implementation steps:**
- Unit tests with mocked responses cover: success, rate limit, timeout, malformed response, partial data
- Integration test (marked `@pytest.mark.integration`) successfully calls the real API
- Provider raises typed exceptions on failure (not generic exceptions)

**For agent implementation steps:**
- Unit tests with mocked LLM responses cover: successful structured output, validation failure with retry, hard failure after retries
- Integration test with real Anthropic call produces valid output schema
- Agent latency logged to Langfuse trace

**For LangGraph node/graph steps:**
- Graph compiles without errors
- Unit test runs graph end-to-end with all nodes mocked
- Checkpointing verified: graph can resume from a checkpoint after simulated crash

**For persistence steps:**
- Migration runs cleanly on empty database
- Migration is idempotent (re-running causes no errors)
- Integration tests against Docker Postgres pass
- All queries use parameterized statements (no string interpolation)

**For infrastructure steps:**
- `cdk synth` produces valid CloudFormation
- `cdk diff` shows expected changes
- `cdk-nag` reports zero unsuppressed errors
- Deploy succeeds without rollback
- Resources visible in AWS console with correct tags

**For CI/CD steps:**
- Workflow runs to green on the PR that introduces it
- All required status checks listed in branch protection rules

**For end-to-end validation steps:**
- The full path produces the expected user-visible output (Telegram message arrives, dashboard view renders, etc.)
- Logs show clean execution (no warnings, no swallowed errors)
- Langfuse trace shows complete reasoning chain

### 5.3 Slice-Level Validation Checkpoints

**End of Slice 1:**
- A Fargate-deployed task runs to completion when manually triggered
- Telegram message arrives in your phone
- Signal record visible in Postgres
- Langfuse trace shows the Analyzer call
- Cost of one scan documented (in dollars)

**End of Slice 2:**
- 7 consecutive days of autonomous operation with no human intervention
- At least 5 signals published, at least 5 skipped
- Forecaster has updated at least one active setup
- Risk gates have prevented at least one rule-violating signal (verified in logs)

**End of Slice 3:**
- Critic has produced its first weekly report as a PR
- At least one Critic-proposed rule change has been merged
- Strategy registry hosts SMC + at least one disabled additional strategy
- Embeddings populated for full journal history

**End of Slice 4:**
- Dashboard accessible at a CloudFront URL with HTTPS
- Cognito auth flow works end-to-end
- All three views render with real data
- WebSocket updates arrive within 2 seconds of state changes
- WAF blocks a test request from a non-allowed region

---

## 6. Additional Recommendations

### 6.1 Process Discipline for Claude Code

**One step per session.** Even if a step feels small, complete it, validate it, commit it, then start a new session for the next step. This prevents context-window bloat and keeps each session focused.

**Always check the spec first.** Before designing anything, re-read the relevant section of this document. This document is the source of truth; intuition is not.

**Write tests before completing a step.** TDD is not always required, but tests must exist before the step is considered complete. Validation without tests is wishful thinking.

**Commit at every step boundary.** Each step is one commit minimum. Squash later if you must, but never let multiple steps blend into one commit.

**Update documentation as you go.** When you change behavior, update the relevant docs file in the same commit. Out-of-date documentation is worse than missing documentation.

### 6.2 Strategy Validation in Parallel

While Claude Code builds Slices 1-2, the human user should spend 30 minutes per day **manually marking SMC signals on TradingView** and tracking outcomes. This is the parallel strategy validation called out in the architecture decision.

If after 2 weeks of manual signals the strategy shows no edge, **pause the build** before investing further in infrastructure. The architecture is correct; the strategy might not be. This is the most important risk to mitigate, and it requires no code.

### 6.3 Cost Discipline

Set AWS Budgets alarms before deploying anything:
- $20/month alarm on dev account
- $150/month alarm on prod account

Set Anthropic API cost alarms via dashboards from day one. LLM costs can spike unexpectedly during prompt iteration. Watch them.

### 6.4 Observability is Not Optional

Langfuse must be running and capturing traces from the very first LLM call. Adding observability later is harder than building it in. Every LLM call, every tool call, every state transition: traced.

### 6.5 Branch Strategy

- `main` — always deployable to dev
- Tag `v*.*.*` — triggers prod deploy with manual approval gate
- `claude/proposed-rules-*` — Critic-authored PRs
- Feature branches — `feat/slice-N-step-M-description`

### 6.6 Documentation Files to Maintain

| File | Purpose | Owner |
|---|---|---|
| `README.md` | Project overview and quick start | Always current |
| `docs/architecture.md` | System architecture diagrams and decisions | Updated when architecture changes |
| `docs/operations.md` | Runbook for production operations | Updated when ops procedures change |
| `docs/strategies.md` | Documentation of each implemented strategy | Updated when strategies added/changed |
| `docs/critic-learnings.md` | Log of accepted rule changes and their impact | Updated by user when accepting Critic PRs |
| `docs/dashboard.md` | Dashboard user guide | Created in Slice 4 |

### 6.7 Things That Will Go Wrong (And What to Do)

| Problem | Likely Cause | First Response |
|---|---|---|
| Agent returns malformed JSON | LLM hallucination | Pydantic validation retry should handle. If repeated, tighten prompt |
| Fargate task fails with permission error | IAM role missing permission | Check CloudWatch logs, add specific permission, redeploy |
| Postgres connection timeouts | Aurora Serverless v2 scaling delay | Acceptable; add connection retry with backoff |
| Binance API geo-blocked | AWS egress IP in wrong region | Move VPC to eu-west-1 or ap-southeast-1 |
| LangGraph state corruption | Schema migration issue | Use checkpointer's ability to inspect state; manually fix in Postgres |
| Telegram messages not arriving | Bot token revoked or chat ID wrong | Verify with `getUpdates` endpoint; rotate token if needed |
| Critic PR not opening | GitHub token expired | Rotate token in Secrets Manager |

### 6.8 What Not to Build

To avoid scope creep, Claude Code should explicitly NOT build these unless asked:

- Order execution / broker integration (signal-only is a hard constraint)
- Mobile app (web dashboard is sufficient)
- Multi-tenancy (single-user for now)
- Backtesting framework (deserves its own design discussion)
- Custom LLM fine-tuning (use base Claude models)
- Alternative LLM providers (Anthropic-only)
- Microservices split (monolith Fargate task is correct for this scale)

### 6.9 When to Pause and Reconsult

Pause the build and consult the human user when:

- A step's complexity is significantly higher than estimated
- A required external service (Binance, FRED, Anthropic) is having an outage
- A test consistently fails for reasons that suggest a spec ambiguity
- Cost projections exceed targets in Section 3.3.5
- A security concern arises that wasn't anticipated in the spec

---

## Appendix A: Quick-Reference Glossary

**SMC** — Smart Money Concepts. Price action methodology based on institutional liquidity engineering.
**BOS** — Break of Structure. Trend continuation signal.
**CHoCH** — Change of Character. Potential reversal signal.
**FVG** — Fair Value Gap. 3-candle price inefficiency.
**OB** — Order Block. Institutional entry zone.
**BSL/SSL** — Buy-Side / Sell-Side Liquidity. Retail stop clusters above highs / below lows.
**OTE** — Optimal Trade Entry. 61.8%-78.6% Fibonacci retracement zone.
**PO3** — Power of 3 / AMD. Daily cycle: Accumulation, Manipulation, Distribution.
**POI** — Point of Interest. A structural zone where price reaction is expected.
**R:R** — Risk-to-Reward ratio.
**HTF / MTF / LTF** — Higher / Mid / Lower Time Frame.

---

## Appendix B: Default Watchlist

Initial watchlist for Slice 1-2 (defined in `src/config/strategies.yaml`):

- BTCUSDT
- ETHUSDT
- SOLUSDT
- BNBUSDT

Expandable later via configuration without code changes.

---

*End of master specification.*
