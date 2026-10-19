# Step 2.14 — Full Pipeline Production Validation (Slice 2 exit gate)

> **Status: superseded — automated by Critic v0** (design
> [`superpowers/specs/2026-09-22-decision-review-critic-v0-design.md`](superpowers/specs/2026-09-22-decision-review-critic-v0-design.md),
> workflow `.github/workflows/critic.yml`, runbook `operations.md §7`). The four exit-gate
> criteria below are now computed by `src/agents/critic/exit_gate.py` and rendered as the
> first section of the `mode=backfill` report (`docs/critic-reports/<date>.md`); the
> per-signal manual review is the `DecisionReviewer` (one Claude call per decision).
> The **manual SQL is kept for reference** and has been corrected for the four pitfalls
> the 2026-09-22 review found (marked ⚠️ below).
> **Authority:** `SPEC.md` §4 Step 2.14 (lines ~678–683) and §5 "End of Slice 2"
> (lines ~928–932). This file is the working procedure, not a substitute for the spec.

---

## 1. The next move, in one line

Pull `scan_runs` / `signals` / `active_setups` / `agent_runs` from Aurora plus the Lambda
logs for the live period, check the four exit-gate criteria, write up the findings.
That closes Slice 2.

## 2. Why it is due now

The system has been live in production (`ap-south-1`) since **2026-06-27**, when a manual
scan returned `{"ok": true}` for all 4 watchlist symbols in parallel, persisted to Aurora
and delivered to Telegram. The repo has had **no commits since** (`HEAD = 76011fa`).

So the one-week observation *window* has long since elapsed — what is outstanding is the
**review**. The first task is therefore to confirm the crons actually kept firing across
that period; an expired Anthropic key, a Telegram token change or a Binance API change
would show up as `FAILED` rows in `scan_runs` or as a gap in the timeline, and would mean
the clock on the 7-consecutive-day criterion restarts from the last healthy run.

## 3. Exit-gate criteria (SPEC §5, "End of Slice 2")

All four must hold before Slice 3 / Step 3.1 starts:

| # | Criterion | Where the evidence lives |
|---|---|---|
| 1 | 7 consecutive days of autonomous operation, no human intervention | `scan_runs` — **16 rows/day** (4 windows × 4 symbols, one row per symbol), all `status = 'SUCCESS'`, none `FAILED` |
| 2 | At least 5 signals **published** and at least 5 **skipped** | published = signal **has an `active_setups` row** (⚠️ `signals.status` is `PUBLISHED` for Judge vetoes too); skipped = no direction, or a direction with no setup row |
| 3 | Forecaster has updated at least one active setup | `active_setups.last_evaluated_at` with `latest_evaluation->>'status'` set (⚠️ the Forecaster writes **no** `agent_runs` rows; ignore rows whose `latest_evaluation->>'source' = 'resolver'`) |
| 4 | Risk gates have prevented at least one rule-violating signal (verified in logs) | `signals` rows with a non-null `payload->>'violated_rule'` (⚠️ the gates write **no** log lines, so the CloudWatch cross-check is unavailable — recorded as a standing finding) |

## 4. Procedure

### 4.0 Live resource handles

From `docs/PROJECT_STATE.md §3` (account `097853039368`, region `ap-south-1`):

| Handle | Value |
|---|---|
| Scan Lambda | `CryptoSignals-Compute-ScanLambdaDD7505A5-4lsj9dosUzzT` |
| Scan log group | `CryptoSignals-Compute-ScanLambdaLogs7DF29218-H5rf5DCKJt4n` |
| Aurora cluster ARN | `arn:aws:rds:ap-south-1:097853039368:cluster:cryptosignals-data-aurora2cbab212-s1pyvq9ztgdm` |
| DB secret ARN | `arn:aws:secretsmanager:ap-south-1:097853039368:secret:crypto-signals/db-a3zZGW` |
| DB name | `signals` |

> The first Data API call after idle may raise `DatabaseResumingException` while Aurora
> wakes — retry after ~8s (`docs/operations.md §4`).

Convenience shell shape for every query below:

```bash
CLUSTER=arn:aws:rds:ap-south-1:097853039368:cluster:cryptosignals-data-aurora2cbab212-s1pyvq9ztgdm
SECRET=arn:aws:secretsmanager:ap-south-1:097853039368:secret:crypto-signals/db-a3zZGW
q() { aws rds-data execute-statement --region ap-south-1 \
        --resource-arn "$CLUSTER" --secret-arn "$SECRET" --database signals \
        --format-records-as JSON --sql "$1"; }
```

### 4.1 Criterion 1 — continuity of autonomous operation

```sql
-- Daily roll-up; look for gaps and FAILED rows.
SELECT date_trunc('day', started_at)                   AS day,
       count(*)                                        AS runs,
       count(*) FILTER (WHERE status = 'SUCCESS')      AS ok,
       count(*) FILTER (WHERE status = 'FAILED')       AS failed,
       count(*) FILTER (WHERE status = 'RUNNING')      AS stuck
FROM scan_runs
GROUP BY 1 ORDER BY 1;
```

⚠️ **Expected cadence: 16 `scan_runs` rows per day**, not four. Each of the four windows
(08:03 / 13:03 / 15:03 / 22:03 UTC) scans the 4-symbol watchlist and `run_scan._persist`
writes **one `scan_runs` row per symbol**. The Forecaster sweep two minutes after each
window writes no `scan_runs` row. A day with fewer than 16 `SUCCESS` rows, or any
`FAILED` row, breaks the streak (`exit_gate.DEFAULT_EXPECTED_SCANS_PER_DAY = 16`).

```sql
-- Every failure, with its message — triage before claiming the streak.
SELECT started_at, session, symbols, error_message
FROM scan_runs WHERE status <> 'SUCCESS' ORDER BY started_at DESC;
```

### 4.2 Criterion 2 — published vs. skipped volume

⚠️ **`signals.status` does not tell you what was published.** `run_scan._persist` writes
`PUBLISHED` for *any* proposal payload — including proposals the **Judge vetoed**
(`agent_runs.output->>'ruling' = 'SKIP'`). Only an `active_setups` row proves a signal
went out. Count published / skipped like this instead of `GROUP BY status`:

```sql
SELECT count(*) FILTER (WHERE a.id IS NOT NULL)                                 AS published,
       count(*) FILTER (WHERE a.id IS NULL AND s.direction IS NULL)             AS skipped_analyzer_or_gate,
       count(*) FILTER (WHERE a.id IS NULL AND s.direction IS NOT NULL)         AS skipped_judge_veto
FROM signals s LEFT JOIN active_setups a ON a.signal_id = s.id;
```

(Recorded as a finding for a follow-up step: give vetoes their own status.) Then the
manual review the step actually calls for — **every** signal, not just the counts:

```sql
SELECT s.created_at, s.symbol, s.status, s.direction,
       s.payload->>'violated_rule'  AS violated_rule,
       s.payload->>'reason'         AS reason,
       s.payload->>'confidence'     AS confidence
FROM signals s ORDER BY s.created_at;
```

For each published signal, read the full `payload` alongside the chart for that symbol and
window, and judge: was the SMC read defensible? Was the R:R honest? Did the Judge's caveat
match the Skeptic's objection? Note prompt weaknesses as you go — tuning those prompts is
in scope for this step.

### 4.3 Criterion 3 — Forecaster actually updating setups

```sql
SELECT a.id, a.signal_id, sg.symbol, a.status, a.opened_at,
       a.last_evaluated_at, a.latest_evaluation
FROM active_setups a JOIN signals sg ON sg.id = a.signal_id
WHERE a.last_evaluated_at IS NOT NULL
  AND a.latest_evaluation ? 'status'
  AND coalesce(a.latest_evaluation->>'source', '') <> 'resolver'
ORDER BY a.opened_at;
```

⚠️ **The Forecaster writes no `agent_runs` rows** — `_run_forecaster` persists only to
`active_setups`, so `SELECT ... FROM agent_runs WHERE agent_role = 'forecaster'` is
always empty and proves nothing. The evidence is `active_setups.last_evaluated_at` with a
STILL_VALID / AT_RISK / INVALIDATED `status` key in `latest_evaluation`. Since Critic v0,
the OutcomeResolver also writes `latest_evaluation` (tagged `"source": "resolver"`);
those rows must not count as Forecaster evidence — hence the filter above.

### 4.4 Criterion 4 — a real risk-gate rejection

```sql
SELECT created_at, symbol,
       payload->>'violated_rule' AS rule,
       payload->>'reason'        AS reason
FROM signals
WHERE status = 'SKIPPED' AND payload->>'violated_rule' IS NOT NULL
ORDER BY created_at;
```

⚠️ **The log cross-check SPEC asks for ("verified in logs") is not possible today.**
`make_risk_gate_node` / `risk_gates.py` emit **no** log line when a rule fires — there is
no `RULE_` marker (or any other) in the Lambda log group, so
`aws logs tail ... --filter-pattern "RULE_"` returns nothing regardless of outcome. The
`SKIPPED` row with `payload->>'violated_rule'` is the only evidence; the automated gate
records this as a standing finding in its evidence string. Follow-up: log a structured
`RULE_<id>` line on every violation (see `PROJECT_STATE.md §7`).

**Caveat to record honestly:** `run_scan` is H4-only (multi-TF / derivatives fetch was
deferred from Step 2.7), so the funding-rate gate (rule 10) is a no-op in production —
see `src/agents/orchestration/risk_gates.py`. A funding-rule rejection therefore cannot
appear live; the criterion has to be satisfied by one of the other nine hard rules.

### 4.5 Delivery-path spot check

`SPEC.md` Step 2.14 also asks to verify **journal accumulation, Forecaster updates and
Telegram delivery**. Journal + Forecaster are covered above. For Telegram, confirm the
published signals from §4.2 each have a corresponding delivery — and if any period looks
silent, fire a manual scan and watch it land:

```bash
aws lambda invoke --function-name CryptoSignals-Compute-ScanLambdaDD7505A5-4lsj9dosUzzT \
  --region ap-south-1 out.json && cat out.json
```
