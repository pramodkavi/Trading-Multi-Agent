# Operations Runbook

> Day-2 operations for the multi-agent crypto-signals system (Step 2.12).
> Architecture and live resource IDs live in [`PROJECT_STATE.md`](PROJECT_STATE.md);
> this is the *how-to* for secrets, deploys, alarms, and incidents.

**Live facts** (full table in `PROJECT_STATE.md §3`):

| | |
|---|---|
| Account / Region | `097853039368` / `ap-south-1` (Mumbai — pinned in `infrastructure/app.py`) |
| Scan Lambda | `CryptoSignals-Compute-ScanLambdaDD7505A5-4lsj9dosUzzT` (zip + layer; replaced at the 2026-10-10 cutover) |
| Scan log group | `CryptoSignals-Compute-ScanLambdaLogs7DF29218-H5rf5DCKJt4n` |
| Aurora cluster ARN | `arn:aws:rds:ap-south-1:097853039368:cluster:cryptosignals-data-aurora2cbab212-s1pyvq9ztgdm` |
| DB secret ARN (Secrets Manager) | `arn:aws:secretsmanager:ap-south-1:097853039368:secret:crypto-signals/db-a3zZGW` |

All commands assume AWS creds for account `097853039368` and `--region ap-south-1`.

---

## 1. Secrets — SSM Parameter Store

As of Step 2.12 the third-party API keys live in **SSM Parameter Store
SecureString** parameters (free standard tier), **not** Secrets Manager. The only
Secrets Manager secret kept is Aurora's own generated DB credential (used by the
RDS Data API), which is managed by the cluster and needs no manual handling.

| Parameter name | Payload | Used by |
|---|---|---|
| `/crypto-signals/anthropic-api-key` | plain string `sk-ant-...`, or JSON `{"api_key":"..."}` | scan Lambda |
| `/crypto-signals/telegram-bot-token` | **JSON** `{"bot_token":"...","chat_id":"..."}` (preferred — carries the chat id) | scan + alarm-notifier Lambdas |

`/crypto-signals/fred-api-key` and `/crypto-signals/twelve-data-api-key` are
reserved names for the optional macro providers; create them only if/when those
keys are in use.

> ⚠️ **CloudFormation cannot create SecureString parameters.** CDK only *references*
> them for the IAM grant. You must create them by hand (below), and they must
> exist **before** the Lambda first runs after a deploy that switched it to SSM —
> otherwise the scan fails with `ParameterNotFound`.

### 1.1 Create the parameters (first-time migration off Secrets Manager)

```bash
# Anthropic key (plain string form)
aws ssm put-parameter --region ap-south-1 \
  --name /crypto-signals/anthropic-api-key \
  --type SecureString \
  --value 'sk-ant-REPLACE_ME'

# Telegram bot token + chat id (JSON form — chat id 8300889332 is the operator)
aws ssm put-parameter --region ap-south-1 \
  --name /crypto-signals/telegram-bot-token \
  --type SecureString \
  --value '{"bot_token":"REPLACE_ME","chat_id":"8300889332"}'
```

This uses the AWS-managed `aws/ssm` KMS key (free); no `--key-id` needed.

### 1.2 Rotate a credential

Rotation is just an overwrite — the running Lambda picks up the new value on its
**next cold start** (no redeploy):

```bash
aws ssm put-parameter --region ap-south-1 --overwrite \
  --name /crypto-signals/anthropic-api-key \
  --type SecureString --value 'sk-ant-NEW'

aws ssm put-parameter --region ap-south-1 --overwrite \
  --name /crypto-signals/telegram-bot-token \
  --type SecureString --value '{"bot_token":"NEW","chat_id":"8300889332"}'
```

To force the new value immediately (instead of waiting for a cold start), publish
a trivial config update or invoke after a few minutes idle. Also update the local
`.env` so dev matches.

> 🔴 **Standing action item:** the Anthropic key and Telegram bot token appeared in
> chat during early development — rotate both at the provider (console.anthropic.com
> / @BotFather), then run the commands above. Old Secrets Manager copies are
> removed by the Step 2.12 deploy.

### 1.3 Decommission the old Secrets Manager API-key secrets

The Step 2.12 deploy removes the `crypto-signals/anthropic-api-key` and
`crypto-signals/telegram-bot-token` **Secrets Manager** secrets (DataStack no
longer creates them). If a deploy left them behind (e.g. retained), delete them:

```bash
aws secretsmanager delete-secret --region ap-south-1 \
  --secret-id crypto-signals/anthropic-api-key --force-delete-without-recovery
aws secretsmanager delete-secret --region ap-south-1 \
  --secret-id crypto-signals/telegram-bot-token --force-delete-without-recovery
```

(These are the no-leading-slash Secrets Manager names; the new SSM names have a
leading `/`.)

---

## 2. Deploy

CD deploys on merge to `main` (dev) and on a `v*.*.*` tag (prod, with a manual
approval gate). **Hard rule: the dev machine has no AWS CLI, CDK CLI or AWS
credentials — every deploy and every one-off AWS operation runs in GitHub Actions.**
No Docker is needed anywhere: CDK builds the zip code asset and the dependency
layer with pip on the runner.

Manual deploy (same workflow, from the GitHub UI): Actions → **Deploy (dev)** →
**Run workflow** → branch `main`. Tick **recreate_stateless_stacks** ONLY when a
function must be replaced (e.g. the 2026-10-03 image→zip repackage): it destroys
`CryptoSignals-Monitoring`, `-Scheduling`, `-Compute` and recreates them (Data and
Network untouched; the two Lambda log groups are lost).

Locally you can only synthesise (no credentials needed) to check cdk-nag and the
asset size guards: `cd infrastructure && python app.py`.

If synth fails with `AssetTooLargeError`, a dependency pushed the layer over the
240 MB guard (or the code over 2.5 MB); the message names the largest packages.
Trim the dependency or move it to an optional extra — do not raise the guard.

Stacks (dependency order): `Network → Data → Compute → Scheduling → Monitoring → Dashboard`.
`CryptoSignals-Dashboard` (Slice 4, §8) is declared last and depends only on `Data`;
`cdk deploy --all` creates it on the first deploy after Step 4.1 merges — no workflow
change. Synth now builds two dependency layers (scan + dashboard), adding ~2 min.
First-time-on-this-account only: `cdk bootstrap` (already done for ap-south-1).

> If switching off Secrets Manager for the first time, **create the SSM
> parameters (§1.1) before the deploy** so the post-deploy scan can read them.

### 2.1 Database schema migration

The CD deploy workflow runs `scripts/migrate.py` (the idempotent `schema.sql`,
all `CREATE/ALTER ... IF NOT EXISTS`) over the Data API **before** `cdk deploy`.
That step is gated by `aws cloudformation describe-stacks` using the OIDC deploy
role; if the role can't make that call directly it **silently skips** (logging
"skipping — first deploy?"), so schema changes never reach the live DB and scans
then fail with `relation "active_setups" does not exist` /
`column "tags" ... does not exist`.

Escape hatch (no AWS CLI needed) — apply the schema with the **Lambda's own
role**, which has Data API + DB-secret access:

```jsonc
// Lambda console → Test → invoke the scan function with:
{ "mode": "migrate" }
// -> {"ok": true, "mode": "migrate", "statements": N}
```

It wakes Aurora through the retried read path first, then applies every
statement; idempotent, so it's safe to run anytime the schema looks behind. Run
it once after any deploy that adds tables/columns until the CD step's IAM is
fixed (give the deploy role `cloudformation:DescribeStacks`,
`rds-data:ExecuteStatement` on the cluster, and `secretsmanager:GetSecretValue`
on the DB secret — see PROJECT_STATE §7).

---

## 3. Alarms (NFR-2.2)

CloudWatch alarms publish to an SNS topic, which a small **notifier Lambda**
(`AlarmNotifier`, same zip code asset + layer as the scan Lambda) forwards to the operator's Telegram bot.
No manual SNS subscription is needed — CDK wires it.

| Alarm | Fires when | Source |
|---|---|---|
| `ScanFailureRateAlarm` | scan Lambda error rate > 10% over 24h | Lambda Errors/Invocations |
| `ScanLatencyP95Alarm` | scan p95 duration > 2 min | Lambda Duration p95 |
| `AuroraCpuAlarm` | Aurora CPU > 80% for 15 min | RDS CPUUtilization |
| `ProviderErrorAlarm` | ≥ 3 provider errors in 1h | Logs metric filter on `PROVIDER_ERROR` |
| `ForecasterErrorAlarm` | ≥ 1 Forecaster per-setup failure in 1h | Logs metric filter on `PROVIDER_ERROR` + `provider=forecaster` |

**Low-volume note:** at ~5 scans/day the failure-rate and provider-error alarms are
effectively "any failure/several provider errors today" — intended sensitivity for
a system the operator trades from. Richer provider-error tracking lands in Step 2.13.

Forecaster per-setup failures (Step 4.0): the sweep runs in the same Lambda/log
group and logs the marker
`PROVIDER_ERROR provider=forecaster setup=<id> error=<Type>: <msg>` for every setup
whose evaluation raises (and journals an `ERROR` row in `setup_evaluations`). The
dedicated `ForecasterErrorAlarm` fires on the **first** such failure in an hour —
the Forecaster emits at most one marker per open setup per sweep (cap 3), so the
shared threshold of 3 is unreachable with 1–2 open setups and a dead Anthropic key
or exhausted credit would otherwise never alarm. The shared `ProviderErrorAlarm`
still counts these markers too.

### Test alarm firing (deliberate failure)

Set an alarm to ALARM directly and confirm the Telegram message arrives, then
reset:

```bash
aws cloudwatch set-alarm-state --region ap-south-1 \
  --alarm-name <AuroraCpuAlarm-physical-name> \
  --state-value ALARM --state-reason "runbook test"
# ... expect a Telegram alert within ~1 min ...
aws cloudwatch set-alarm-state --region ap-south-1 \
  --alarm-name <AuroraCpuAlarm-physical-name> \
  --state-value OK --state-reason "runbook test reset"
```

Find the physical alarm names with
`aws cloudwatch describe-alarms --region ap-south-1 --alarm-name-prefix CryptoSignals-Monitoring`.

A real end-to-end test: point `ANTHROPIC_PARAM_NAME` at a bad parameter, invoke
the scan a few times so it errors, and watch `ScanFailureRateAlarm` /
`ProviderErrorAlarm` trip (then revert).

---

## 4. Routine operations

### 4.1 Editing and testing in the Lambda console

Both Lambdas are zip-packaged, so the console shows the full `src/` and
`scripts/` tree (design 2026-10-03). Workflow:

1. Lambda console → the scan function → **Code** tab. Edit any file (agent
   prompts, the watchlist default in `src/config/settings.py`, risk gates in
   `src/agents/orchestration/risk_gates.py`, pipeline logic, extra logging).
2. **Deploy** (console button, a few seconds).
3. **Test** tab. Useful saved events:

   | Event | What it runs |
   |---|---|
   | `{}` | full watchlist scan |
   | `{"symbols": ["BTCUSDT"]}` | one symbol |
   | `{"mode": "forecaster"}` | Forecaster sweep over open setups |
   | `{"mode": "resolve", "chunk_size": 25}` | Critic v0 outcome resolver — **only after the Critic v0 branch (Step 2.14) is merged**; on earlier code an unknown `mode` falls through to a full scan |
   | `{"mode": "migrate"}` | apply the DB schema over the Data API |
   | `{"symbols": ["BTCUSDT"], "session": "AD_HOC"}` | one symbol with the rule-7 session window bypassed (manual smoke test only) |

   The JSON summary appears inline; logs under **Monitor → View CloudWatch logs**.
   First call after idle may hit Aurora resuming — retry after ~8 s.

   **Scan session (Step 4.0, design §14 D1).** Since Step 4.0 the scan derives
   its `ScanSession` from the UTC clock at scan start (Asian 00:00–08:00, London
   08:00–13:00, NY 13:00–15:00, Overlap 15:00–21:00, Cooldown 21:00–00:00) and
   records it on `scan_runs.session`, so SPEC §1.6 rule 7 now actually fires:
   **the 22:03 daily-wrap scan runs inside Cooldown and is review-only** — it
   still journals (`scan_runs` / `signals` / `agent_runs`), but any new
   proposal is force-skipped with `RULE_7_SESSION_BLOCK`. Open setups are still
   reviewed at daily wrap, but by the **separate 22:05 `{"mode":"forecaster"}`
   sweep** (the `ForecasterSweep` schedule), not by the 22:03 scan itself; the
   sweep never proposes, so rule 7 does not touch it. Scheduled invocations
   never set `session`. A console Test event may add `"session": "AD_HOC"`
   (case-insensitive) to bypass the window for a manual smoke test; an unknown
   value fails the invocation on purpose rather than silently bypassing the rule.

**Caveats**
- **Every `cdk deploy` (manual, or the push-to-main auto-deploy) replaces the
  function code with the git contents.** Copy anything you want to keep into the
  repo by hand first. Git is the source of truth for deploys; the console is a
  scratchpad.
- A new third-party import that is not in the dependency layer fails at init
  (`ImportError`) and trips `ScanFailureRateAlarm` → Telegram. Add it to
  `pyproject.toml` and deploy from git instead.
- A syntax error behaves the same way; fix it in the console or redeploy.

### 4.2 CLI equivalents

```bash
# Manually invoke the scan Lambda (empty payload = full watchlist).
aws lambda invoke --function-name <scan-fn-name> --region ap-south-1 out.json && cat out.json
#  ^ first call after idle may throw DatabaseResumingException (Aurora waking) — retry ~8s.

# Run only the Forecaster pass.
aws lambda invoke --function-name <scan-fn-name> --region ap-south-1 \
  --payload '{"mode":"forecaster"}' out.json && cat out.json

# Tail scan logs.
aws logs tail <scan-log-group> --region ap-south-1 --follow

# Tail the alarm-notifier logs (debug missing Telegram alerts).
aws logs tail /aws/lambda/<AlarmNotifier-fn-name> --region ap-south-1 --follow
```

---

## 5. Incident playbook

| Symptom | Likely cause | Action |
|---|---|---|
| Scan fails `ParameterNotFound` | SSM param missing/renamed | Create it (§1.1); confirm the name matches `infrastructure/stacks/parameters.py` |
| Scan fails `AccessDenied` on ssm | role missing the grant for that param | Confirm the param name is one of the two granted in `ComputeStack`; redeploy |
| No Telegram alerts on a known alarm | notifier Lambda erroring | Tail its logs (§4); usually missing/invalid `/crypto-signals/telegram-bot-token` |
| First Data API call after idle times out | Aurora scale-to-zero waking | Expected; retry with backoff (~8s) |
| `ScanLatencyP95Alarm` | slow LLM calls / large watchlist | Check Anthropic status + Langfuse traces; consider trimming the watchlist |
| `AuroraCpuAlarm` | runaway query / migration | Inspect Data API usage; CPU should idle near 0 at this volume |
| Telegram `401/403` in scan logs | bot token revoked / bot blocked | Rotate the token (§1.2); confirm the bot isn't blocked in the chat |

See `SPEC.md §6.7` for the broader failure-mode table.

---

## 6. Cost watch

Targets: ~$5–9/month (tight). Drivers: Aurora ACU-hours (scale-to-zero keeps this
near zero when idle), Anthropic tokens (~5 scans/day), Lambda (negligible), S3.
SSM standard parameters and the SNS/alarm path are free. Critic v0 (§7) records
every review call's cost in `agent_runs` (`agent_role = 'critic'`) and stops a
backfill after its first chunk until the operator confirms the extrapolated
spend.

**AWS Budget guardrail** (`MonitoringStack`, `MonthlyCostBudget`): a **$15/month**
COST budget on the account. It notifies the alarm SNS topic — the same path the
CloudWatch alarms use, so the message lands in **Telegram** via the notifier
Lambda — on two conditions:

| Notification | Fires when | Meaning |
|---|---|---|
| ACTUAL > 80 % | month-to-date spend passes **$12** | something is already burning money |
| FORECASTED > 100 % | AWS's month-end forecast passes **$15** | the trend will blow the budget |

AWS Budgets data lags billing by **~8–12 hours** (budgets are evaluated a few
times a day, not continuously), so this is a guardrail against runaway spend,
not a real-time alarm — a stuck Aurora at max ACU can run for half a day before
the message arrives. Still watch the AWS Billing console weekly; investigate if
the monthly run-rate exceeds ~$10. Anthropic token spend is not in this budget
(it is tracked in `agent_runs`, NFR-5.1).

---

## 7. Critic v0 (decision review) — Step 2.14

Design: [`superpowers/specs/2026-09-22-decision-review-critic-v0-design.md`](superpowers/specs/2026-09-22-decision-review-critic-v0-design.md).
Critic v0 scores every decision the pipeline has made against real price action,
grades the reasoning with Claude, and delivers a report + proposed-rules PR. It is
**all-cloud**: nothing runs on a laptop.

### 7.1 What runs where

| Layer | Runs in | Trigger | Writes |
|---|---|---|---|
| **OutcomeResolver** (deterministic replay of entry / stop / target on 5m Binance candles) | the scan Lambda, `{"mode":"resolve","chunk_size":25}` (Mumbai — Binance geofences GitHub runners with HTTP 451) | EventBridge `rate(2 days)` **and** the resolve loop in `critic.yml` | `decision_resolutions`, `signals.outcome` (published only), `active_setups.latest_evaluation` (`"source":"resolver"`) |
| **DecisionReviewer** (one Claude call per resolved decision) | GitHub Actions, `python -m scripts.run_critic review --chunk-size 25` | `.github/workflows/critic.yml` — cron `30 22 */2 * *` (every other day, after the 22:03 UTC wrap scan) or `workflow_dispatch` | `decision_reviews`, `agent_runs` (`agent_role='critic'`, cost) |
| **CriticReporter** (period aggregates + one Claude `CriticReport` call + markdown) | GitHub Actions, `python -m scripts.run_critic report` | same workflow, after the review loop | `critic_reports`, `docs/critic-reports/<date>.md`, PR on `claude/proposed-rules-<date>`, Telegram summary |

Both layers are idempotent and chunked (25 decisions per chunk, at most
`max_chunks` = 40 chunks per run); the queue is the data itself, so an
interrupted run simply continues on the next one.

Workflow phases, in order: resolve ARNs + read the two SSM secrets → apply the
schema via the Lambda `{"mode":"migrate"}` → resolve loop until `remaining == 0`
→ review loop until `remaining=0` → report + PR + Telegram → upload the markdown
as the `critic-report` artifact (even on failure).

### 7.2 Dispatching a backfill (first run / Slice 2 exit gate)

The scheduled run is `mode=incremental` (period = last 14 days, no exit gate).
The **first** run should be a backfill over everything since go-live:

```bash
# 1. First chunk only: stops after 25 reviews and prints the observed cost/review
#    plus the extrapolated total for the remaining queue (cost guard, design §7.3).
gh workflow run critic.yml -f mode=backfill

# 2. Read the ::notice:: in the run log. If the estimate is acceptable
#    (design §7.3: <= 25% of the $200/month LLM ceiling), continue:
gh workflow run critic.yml -f mode=backfill -f confirm_cost=true

# Optional: narrower period, or a bigger chunk budget for a long queue.
gh workflow run critic.yml -f mode=backfill -f confirm_cost=true \
  -f period_start=2026-07-01 -f max_chunks=80
```

`mode=backfill` sets the period start to `2026-06-12T00:00:00Z` (go-live) and
adds `--exit-gate`, so the report's first section is the **Slice 2 exit gate**
(SPEC §5 "End of Slice 2") with PASS / FAIL per criterion and evidence counts.

Inputs: `mode` (`incremental` | `backfill`), `confirm_cost` (bool, backfill
only), `max_chunks` (default `40`), `period_start` (ISO date `YYYY-MM-DD`, taken
as 00:00 UTC, or a full `YYYY-MM-DDTHH:MM:SSZ`; empty = 14 days ago for
incremental, 2026-06-12 for backfill). The workflow widens a bare date to
`T00:00:00Z` before calling `run_critic report --since`, which rejects any
timestamp without a `Z` / offset — the period bounds decide which decisions a
report covers, so an ambiguous bound is an error rather than "assume UTC".

### 7.3 Where reports land

- `docs/critic-reports/<YYYY-MM-DD>.md` on branch `claude/proposed-rules-<YYYY-MM-DD>`,
  force-pushed by `critic-bot`; a PR against `main` titled `Critic report <date>` carries
  the report as its body (re-runs on the same day update the same PR).
- The `critic-report` workflow artifact (same markdown), uploaded even when a later step fails.
- The `critic_reports` table (JSON + markdown) — the source of truth if the PR is closed.
- A Telegram summary (first 300 characters + proposed-change count + gate verdict).

Pushes made with `GITHUB_TOKEN` do **not** trigger CI on the report branch; that is
expected (the branch only carries one markdown file). Review the PR, then merge or
close it — merging only adds the report file; proposed rule changes are text for a
human to implement in a follow-up step.

### 7.4 IAM — the OIDC deploy role (operator action, outside the repo)

`critic.yml` assumes the same role as `deploy-dev.yml` (`AWS_DEPLOY_ROLE_ARN`). On
top of the CDK deploy permissions it needs the statements below. Attach as an inline
policy (account `097853039368`, region `ap-south-1`); this also unblocks the CD
pre-deploy migration, which today silently skips because the role cannot
`DescribeStacks` (§2.1).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadStackOutputs",
      "Effect": "Allow",
      "Action": "cloudformation:DescribeStacks",
      "Resource": [
        "arn:aws:cloudformation:ap-south-1:097853039368:stack/CryptoSignals-Data/*",
        "arn:aws:cloudformation:ap-south-1:097853039368:stack/CryptoSignals-Compute/*"
      ]
    },
    {
      "Sid": "InvokeScanLambda",
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:ap-south-1:097853039368:function:CryptoSignals-Compute-ScanLambdaDD7505A5-4lsj9dosUzzT"
    },
    {
      "Sid": "AuroraDataApi",
      "Effect": "Allow",
      "Action": ["rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement"],
      "Resource": "arn:aws:rds:ap-south-1:097853039368:cluster:cryptosignals-data-aurora2cbab212-s1pyvq9ztgdm"
    },
    {
      "Sid": "ReadDbCredential",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:ap-south-1:097853039368:secret:crypto-signals/db-a3zZGW"
    },
    {
      "Sid": "ReadApiKeys",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": [
        "arn:aws:ssm:ap-south-1:097853039368:parameter/crypto-signals/anthropic-api-key",
        "arn:aws:ssm:ap-south-1:097853039368:parameter/crypto-signals/telegram-bot-token"
      ]
    }
  ]
}
```

The SSM parameters use the AWS-managed `aws/ssm` key, so no `kms:Decrypt`
statement is needed. If they are ever moved to a customer-managed key, add
`kms:Decrypt` on that key's ARN. If the Lambda is ever replaced, update the
`InvokeScanLambda` resource (the workflow reads the current name from the
`CryptoSignals-Compute` `ScanFunctionName` output and falls back to the name above).

Also required (GitHub side): repo secret `AWS_DEPLOY_ROLE_ARN`, repo variable
`AWS_REGION = ap-south-1`. The PR is opened with the job's `GITHUB_TOKEN`; no
GitHub PAT is stored anywhere.

### 7.5 Cost guard

Anthropic spend for a backfill is unknown until measured. With `mode=backfill` and
`confirm_cost=false` (default) the job exits **0** after the first 25 reviews and
prints `::notice::first backfill chunk cost $X (~$Y/review); ~N remaining -> est $Z`.
The report step is skipped on that run. Nothing is lost: the reviews already made are
in `decision_reviews`, and the next run continues from the queue. Every review's
`cost_usd` also lands in `agent_runs` (`agent_role = 'critic'`), so spend can be
audited with:

```sql
SELECT date_trunc('day', created_at) AS day, count(*) AS reviews, sum(cost_usd) AS usd
FROM agent_runs WHERE agent_role = 'critic' GROUP BY 1 ORDER BY 1;
```

### 7.6 Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `cfg` step: `AccessDenied` on `DescribeStacks` / `ssm:GetParameter` | IAM grants from §7.4 missing | Attach the policy; re-run |
| `Apply schema` step fails (`grep "ok": true`) | Lambda migrate returned an error (Aurora resuming beyond the retry budget, or the dataapi backend not configured) | `cat out.json` in the log; re-run — migrate is idempotent |
| Resolve loop: `remaining` never reaches 0, `resolved=0` → `::warning::no progress` | every remaining decision is deferred — usually Binance `PROVIDER_ERROR` (rate limit / outage) or candles not yet available for very recent decisions | `aws logs tail <scan-log-group> --since 1h --filter-pattern PROVIDER_ERROR`; recent decisions resolve on the next run; a persistent 451 means the Lambda region changed |
| Resolve loop stops at `max_chunks` | large backfill queue | re-dispatch with `-f max_chunks=80`; the loop is idempotent |
| Review loop: `REVIEW ... failed=N` | Claude call errors after retries; the rows carry `decision_reviews.error` | they are retried automatically once older than 1 day; persistent failures show up in the report's failure table |
| Review loop exits 2 immediately | store / API unreachable (bad `DB_*` env, expired Anthropic key) | check the `cfg` step output; rotate the key (§1.2) |
| No PR, but the report is in the artifact | `gh pr create`/`push` failed — branch protection or missing `contents: write` | the job fails so it is visible; the report is also in `critic_reports`; fix and re-run (same-day re-run updates the same PR) |
| No Telegram message but the PR exists | notifier failure is non-fatal by design | see the `telegram summary failed` traceback in the report step log; usually a bad `/crypto-signals/telegram-bot-token` |
| Two runs at once | impossible by design (`concurrency: critic`) — the EventBridge resolver *may* overlap the Actions resolve loop | harmless: resolution is idempotent per decision |

---

## 8. Dashboard API (Slice 4)

Design: `docs/superpowers/specs/2026-10-18-serverless-dashboard-design.md`
(serverless, read-only Phase 1; its §10 table is SPEC §4 Slice 4).

### 8.1 What the stack is (Step 4.1 scaffold)

`CryptoSignals-Dashboard` (`infrastructure/stacks/dashboard_stack.py`, declared
**last** in `infrastructure/app.py`, coupled only to `CryptoSignals-Data` via the
cluster ARN + DB secret ARN imports):

| Resource | Detail |
|---|---|
| Lambda `DashboardApi` | python3.11, 512 MB, 25 s timeout, **outside the VPC**; handler `src.dashboard.api.app.handler` (FastAPI wrapped by Mangum); env `PERSISTENCE_BACKEND=dataapi`, `DB_CLUSTER_ARN`, `DB_SECRET_ARN`, `DB_NAME`, `APP_VERSION`, `LOG_LEVEL` |
| Its own `DepsLayer` | built by `lambda_assets.deps_layer(..., extras=("dashboard",))` — base deps + `fastapi` + `mangum` (~136 MB); the scan Lambda's layer is unchanged |
| HTTP API `crypto-signals-dashboard` | one route `GET /api/health` (no authorizer yet — Step 4.4 adds the Cognito JWT authorizer); `$default` stage, auto-deploy, throttle 10 rps / burst 20 |
| Log groups | `DashboardApiLogs` (function) + `DashboardApiAccessLogs` (API access log), 14-day retention |
| IAM role | RDS Data API on the one cluster + read of its secret, CloudWatch Logs write — **no SSM, no S3** |
| Outputs | `DashboardApiUrl`, `DashboardFunctionName` |

Nothing else is reachable until Step 4.2+ (queries + overview/setups/signals
endpoints). In 4.1 **nothing in the stack touches Aurora** — the health route does
not open a DB call, so browsing it never wakes the cluster. The Data API grant is
in place for 4.2.

### 8.2 Where the API URL is

CloudFormation console → `CryptoSignals-Dashboard` → **Outputs** → `DashboardApiUrl`
(and `DashboardFunctionName`); or the `cdk deploy` log of the Deploy (dev) run.
Record both in `docs/PROJECT_STATE.md §3` after the first deploy.

### 8.3 Smoke test

```bash
curl -s https://<api-id>.execute-api.ap-south-1.amazonaws.com/api/health
# → {"status":"ok","version":"<short sha>"}
```

`version` is the deployed commit: `GITHUB_SHA[:7]` when built on the runner
(`git rev-parse --short HEAD` for a local synth, else `local`). It must match the
merge commit that triggered the deploy — if it does not, the deploy picked up a
stale asset. OpenAPI is served at `/api/openapi.json`, Swagger UI at `/api/docs`.

A request also produces one line in `DashboardApiAccessLogs`; the fields should be
real values, not all `-` (CDK's default access-log format uses REST-era variables —
if they come out blank, switch to an HTTP-API-specific format in Step 4.4).

### 8.4 Console editing

The function is zip-packaged like the scan Lambda (§4.1): **Code** tab shows
`src/dashboard/api/…`, Deploy, then call the URL. The same caveat applies — any
`cdk deploy` replaces console edits with the git contents. There is no console
**Test** event for an HTTP API Lambda worth saving; use the URL.

### 8.5 Removal

The stack is independent: deleting `CryptoSignals-Dashboard` in CloudFormation
(or `cdk destroy CryptoSignals-Dashboard` from Actions) removes the API, function,
layer and log groups without touching the pipeline stacks.
