# Crypto Signals System

> Multi-agent, autonomous crypto trading **signal analyzer** built on Smart Money Concepts (SMC).
>
> **Signal-only.** This system never places trades — it analyzes markets, produces high-conviction trade ideas through adversarial multi-agent validation, and delivers them to Telegram for manual execution.

See [`SPEC.md`](./SPEC.md) for the authoritative specification and [`CLAUDE.md`](./CLAUDE.md) for the build index.

## Status

Slice 1 (End-to-End Substrate) — in progress. See SPEC.md §4 for the roadmap.

## Quick start

Local development setup will be documented here once Slice 1 Step 1.13 (docker-compose) is complete.

## Architecture

Six Claude-powered agent roles:

- **Analyzer** — runs the SMC 5-layer protocol on raw market data.
- **Historian** — three-stage retrieval over the signal journal.
- **Skeptic** — fetches macro context (DXY, SPX, VIX) and tries to invalidate.
- **Judge** — weighs proposal + history + objection → PUBLISH / PUBLISH_WITH_CAVEAT / SKIP.
- **Forecaster** — re-evaluates open setups on every scan.
- **Critic** — weekly meta-review that opens PRs with rule-change proposals.

## License

Private. Not licensed for redistribution.
