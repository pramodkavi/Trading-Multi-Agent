---
name: architecture-dynamodb-vs-aurora
description: DynamoDB vs Aurora Serverless v2 cost/feasibility analysis for Slice 2 optimization decision
metadata:
  node_type: memory
  type: project
  decision_point: Slice 1 Step 1.22 (deployment paused)
  date_evaluated: 2026-06-06
  originSessionId: f29f3cf9-20ea-45e3-bf96-82b625abdeba
---

# DynamoDB vs Aurora Serverless v2 — Architectural Decision

**Status:** PAUSED at Step 1.22. Aurora Serverless v2 is currently deployed (or will be when creds refresh). This document captures the cost/feasibility analysis for a Slice 2 re-evaluation if actual production costs exceed thresholds.

## The Question

Is DynamoDB a viable cost-optimization over Aurora Serverless v2 for the signal persistence layer?

## TL;DR

**Decision: Deploy Aurora Serverless v2 now (Slice 1). Defer DynamoDB to Slice 2 if costs exceed $10/mo.**

- **DynamoDB cost:** ~$0.50/mo at 1 scan/day (70% cheaper)
- **Aurora cost:** ~$2–5/mo at 1 scan/day
- **Refactoring cost:** 15–20 engineering hours + 100+ test rewrites
- **Cost savings at Slice 1 scale:** $1.50/mo doesn't justify the refactor
- **Revisit point:** If Slice 2 (Forecaster + Critic) pushes Aurora costs above $10/mo consistently, schedule a DynamoDB rewrite

---

## Current Data Model (PostgreSQL/Aurora)

### Schema (schema.sql)

```sql
-- scan_runs: one row per scan execution
CREATE TABLE scan_runs (
    id UUID PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    status TEXT CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED')),
    error_message TEXT,
    session TEXT,
    strategy TEXT,
    symbols TEXT[]  -- <-- Array type; no native DynamoDB equivalent
);
CREATE INDEX idx_scan_runs_started_at ON scan_runs (started_at DESC);

-- signals: every analyzer output (published or skipped)
CREATE TABLE signals (
    id UUID PRIMARY KEY,
    scan_id UUID NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,  -- <-- FK
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT CHECK (direction IN ('LONG', 'SHORT')),
    status TEXT CHECK (status IN ('PUBLISHED', 'SKIPPED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL  -- <-- Complex nested object
);
CREATE INDEX idx_signals_scan_id ON signals (scan_id);
CREATE INDEX idx_signals_created_at ON signals (created_at DESC);
CREATE INDEX idx_signals_symbol_created_at ON signals (symbol, created_at DESC);  -- <-- Multi-column
CREATE INDEX idx_signals_status ON signals (status);

-- agent_runs: per-agent execution log
CREATE TABLE agent_runs (
    id UUID PRIMARY KEY,
    scan_id UUID NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,  -- <-- FK
    agent_role TEXT CHECK (agent_role IN (...)),
    strategy TEXT,
    input_hash TEXT NOT NULL,
    output JSONB NOT NULL,
    latency_ms INTEGER NOT NULL,
    token_usage JSONB NOT NULL DEFAULT '{}',
    cost_usd NUMERIC(12, 6),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
Create indexes on (scan_id), (agent_role, created_at DESC), (created_at DESC);
```

### Access Patterns (from repositories.py)

| Pattern | Frequency | Query Type | Current Index |
|---------|-----------|-----------|-----------------|
| `get_scan_run(scan_id: UUID)` | Frequent | Single PK lookup | PK |
| `get_signal(signal_id: UUID)` | Frequent | Single PK lookup | PK |
| `list_recent_signals(limit: int, offset: int)` | Daily per scan | Range query on created_at | `idx_signals_created_at` |
| `list_signals_by_symbol_recent(symbol, limit, offset)` | Daily per symbol | **Composite** on (symbol, created_at) | `idx_signals_symbol_created_at` |
| `list_signals_by_status(status)` | Occasional (analytics) | Filtered scan on status | `idx_signals_status` |
| `log_agent_run(scan_id, agent_role, ...)` | Once per agent per scan | Insert + return | PK on creation |
| **Cascade delete** | When scan completes | FK constraint | ON DELETE CASCADE |

### Critical Properties

1. **Foreign key constraints**: signals.scan_id → scan_runs.id, agent_runs.scan_id → scan_runs.id, with `ON DELETE CASCADE`. If a scan_runs row is deleted, all dependent signals + agent_runs rows must automatically be removed.
2. **JSONB payloads**: The `payload` column in signals stores the full `SignalProposal` or `SkipDecision` Pydantic model as JSON. Not currently queried for field extraction (that happens in application code), but Step 3.4 may add embeddings searches.
3. **Text arrays**: `symbols` in scan_runs is a PostgreSQL TEXT[] (variable-length array).
4. **Multi-column indexes**: The `(symbol, created_at DESC)` composite index is critical for the Historian agent's "recent signals by symbol" query.

---

## Cost Analysis

### Aurora Serverless v2 (Current)

**Pricing:**
- ACU (Aurora Capacity Unit): $0.29 / ACU-hour (HA); $0.145 / ACU-hour (single-AZ)
- Data storage: $0.10 / GB-month
- Backup (automated): included in storage
- RDS Data API: bundled, tiny per-call fee (~$0.0000075 per request; effectively free)

**Slice 1 Scenario (1 scan/day):**
- Scan: 1 per day = 365/year
- Writes per scan: ~3 (1 scan_run INSERT/UPDATE, 1 signal INSERT, 1 agent_run INSERT) = ~1,095 writes/year
- Reads per scan: ~5 (during analysis + Historian lookups) = ~1,825 reads/year
- **ACU usage**: With scale-to-zero, the cluster auto-scales to 0 ACU when idle (no compute). On-demand scaling to ~0.5–1 ACU during the ~60-second scan window.
  - 60 seconds × 365 days = 21,900 seconds / 3,600 = ~6 ACU-hours/year
  - Cost: 6 × $0.145 = **~$0.87/month** (HA mode: $1.74/month)
- **Storage**: ~50–100 MB/year = negligible
- **Total: ~$2–5/month** (depending on HA vs single-AZ, and growth rate)

**Slice 2 Scenario (Forecaster + Critic; assume 5 scans/day + 1 critic run/week):**
- ~5 scans × 365 = ~1,825 scans/year
- ~50 ACU-hours/year (more complex queries from Forecaster/Critic)
- Cost: ~$7–14/month

### DynamoDB On-Demand

**Pricing:**
- **Writes**: $1.25 per million write units (WU)
- **Reads**: $0.25 per million read units (RU)
- **Data storage**: $0.25 per GB-month
- **GSI writes/reads**: same as table

**Slice 1 Scenario (1 scan/day):**
- ~1,095 writes/year (scan_runs, signals, agent_runs) → ~$0.0014/mo
- ~1,825 reads/year (lookups + list queries) → $0.00046/mo
- **Storage**: ~50–100 MB → ~$0.01–0.025/mo
- **GSI overhead** (3–4 GSIs required; each duplicates writes): 3 × 1,095 writes/year → $0.0042/mo
- **Total: ~$0.05–0.20/month** (nearly free)

**On-Demand summary:** DynamoDB at this scale is **~10–20x cheaper** ($0.50/mo vs $2–5/mo).

### DynamoDB Provisioned (More Realistic)

If you want to avoid surprise costs from spikes:
- **Min provisioning**: 5 RCU + 5 WCU + 3 GSI replicas
- **Cost**: ~$5–7/month (same as Aurora Serverless v2!)

**Provisioned summary:** If you don't want to worry about spike billing, provisioned DynamoDB isn't actually cheaper.

---

## Feasibility Analysis

| Requirement | Aurora | DynamoDB | Effort to Port |
|---|---|---|---|
| **Foreign key constraints** | ✅ Native | ❌ None | Add app-level cascade logic in delete methods |
| **ON DELETE CASCADE** | ✅ Atomic | ❌ Manual | For each `delete_scan_run()`, explicitly delete signals + agent_runs |
| **JSONB queryability** | ✅ Full SQL | ⚠️ Read-only | OK for now; Step 3.4 embeddings would need app-side filtering |
| **TEXT[] (arrays)** | ✅ Native | ⚠️ Convert to List | Transform in/out of DynamoDB format |
| **Composite indexes** (symbol + created_at) | ✅ Single composite | ⚠️ GSI | Need a dedicated GSI just for this pattern |
| **Multi-index queries** | ✅ Single table | ⚠️ Multiple GSIs | 4 GSIs needed: by scan_id, by created_at, by (symbol, created_at), by status |
| **Strong consistency** | ✅ Default | ⚠️ Costs extra | Need `ConsistentRead=True` for some queries; adds latency + WRCUs |
| **Pagination** (offset/limit) | ✅ Simple | ⚠️ Token-based | DynamoDB's ExclusiveStartKey is less intuitive than SQL OFFSET/LIMIT |

### GSI Requirements for DynamoDB

To match current queries, you'd need:

```
Table: signals
  PK: id (UUID)
  GSI1:
    PK: scan_id (UUID)
    SK: created_at (TIMESTAMPTZ)
  GSI2:
    PK: created_at (TIMESTAMPTZ)
    SK: id (UUID)
  GSI3:
    PK: symbol (TEXT)
    SK: created_at (TIMESTAMPTZ)  # <-- Critical for Historian agent
  GSI4:
    PK: status (TEXT)
    SK: created_at (TIMESTAMPTZ)
```

Each GSI replicates writes (so 4 × 1,095 writes/year instead of 1 × 1,095). On-demand, this adds cost; provisioned, it adds capacity requirements.

---

## Refactoring Scope

### Code Changes Required

1. **Replace SignalStore Protocol implementations**
   - Rewrite `AsyncpgSignalStore` (local dev) → `DynamoDBSignalStore`
   - Remove `DataApiSignalStore` (Aurora Data API) → not needed for DynamoDB
   - ~400 lines of SQL → ~600 lines of boto3 DynamoDB API calls

2. **Cascade logic in persistence layer**
   ```python
   async def delete_scan_run(self, scan_id: UUID) -> None:
       # OLD (Aurora): DELETE FROM scan_runs WHERE id = $1;  -- FK cascades
       # NEW (DynamoDB):
       signal_ids = await self.list_signals_by_scan_id(scan_id)
       agent_run_ids = await self.list_agent_runs_by_scan_id(scan_id)
       # Batch delete signals + agent_runs + scan_run
       await self.batch_delete([*signal_ids, *agent_run_ids, scan_id])
   ```

3. **Query translation**
   - `SELECT ... WHERE created_at DESC LIMIT 10` → `query(GSI2, Limit=10, ScanIndexForward=False)`
   - `SELECT ... WHERE symbol = ? AND created_at DESC` → `query(GSI3, KeyConditionExpression="symbol = ?", ...)`
   - Complex filter expressions for multi-condition queries

4. **Tests**
   - Current: 100+ unit tests in `tests/unit/test_*_store.py` using mocked asyncpg/boto3
   - New: Rewrite all tests to mock DynamoDB (or use LocalStack)
   - Estimate: 20–30 hours

### Estimated Effort

| Task | Hours |
|------|-------|
| Port SignalStore to DynamoDB | 8–10 |
| Add cascade delete logic | 3–4 |
| Query translation + testing | 5–7 |
| Integration test rewrites | 4–6 |
| **Total** | **20–27 hours** |

---

## Decision Framework for Slice 2

### **Trigger: Revisit DynamoDB if…**

1. **Actual Aurora costs exceed $10/month consistently** (after 2–3 weeks of production data).
   - Check CloudWatch RDS metrics: look at Average ACU and data volume.
   - If it's consistently $12+/mo, the refactor has a 6-month payback horizon.

2. **Data volume grows beyond Aurora's scale-to-zero efficiency.**
   - If agent_runs table grows to >1 GB and we're keeping 90-day retention, Aurora's storage costs ($0.10/GB) add up.
   - DynamoDB's on-demand model scales linearly with usage.

3. **Multi-region replication is required** (for resilience).
   - DynamoDB Global Tables are simpler than Aurora multi-region failover.
   - Unlikely for Slice 1–2; more relevant for production hardening.

### **Trigger: Keep Aurora if…**

1. **Costs stay below $5/month** (very likely at Slice 1 scale).
2. **Codebase complexity matters more than cost** (current state).
3. **Step 3.4 embeddings** require semantic search on JSONB payloads.
   - Aurora + pgvector can do this natively.
   - DynamoDB would need external vector DB or app-side filtering (more complex).

---

## Recommendation Summary

| Scenario | Action |
|----------|--------|
| **Slice 1 (now)** | Deploy Aurora Serverless v2. Monitor costs for 3 weeks. |
| **Slice 2 (if Aurora > $10/mo)** | Schedule 2–3 day DynamoDB rewrite sprint. Priority: medium. |
| **Slice 2 (if Aurora < $5/mo)** | Keep Aurora. Allocate savings to other Slice 2 features (Forecaster, Critic, dashboard). |
| **Slice 3 (if embeddings)** | Evaluate Aurora + pgvector for semantic search. DynamoDB would require external vectorDB. |

---

## References

- **SPEC.md § 3.3**: Persistence requirements and trade-offs
- **SPEC.md § 3.3.5**: Cost targets (under $50/mo for MVP)
- **schema.sql**: Current DDL
- **src/persistence/repositories.py**: Access pattern definitions
- **tests/unit/test_*_store.py**: Coverage and test patterns (would need rewriting)

---

## Decision Log

- **2026-06-06**: Evaluated DynamoDB during Step 1.22 deploy walkthrough. Concluded: Aurora Serverless v2 now, DynamoDB in Slice 2 if justified by costs. Documented in [[architecture-dynamodb-vs-aurora]].
