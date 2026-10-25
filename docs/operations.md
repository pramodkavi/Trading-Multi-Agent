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

### 8.1 What the stack is (since Step 4.1)

`CryptoSignals-Dashboard` (`infrastructure/stacks/dashboard_stack.py`, declared
**last** in `infrastructure/app.py`, coupled only to `CryptoSignals-Data` via the
cluster ARN + DB secret ARN imports):

| Resource | Detail |
|---|---|
| Lambda `DashboardApi` | python3.11, 512 MB, 25 s timeout, **outside the VPC**; handler `src.dashboard.api.app.handler` (FastAPI wrapped by Mangum); env `PERSISTENCE_BACKEND=dataapi`, `DB_CLUSTER_ARN`, `DB_SECRET_ARN`, `DB_NAME`, `APP_VERSION`, `LOG_LEVEL` |
| Its own `DepsLayer` | built by `lambda_assets.deps_layer(..., extras=("dashboard",))` — base deps + `fastapi` + `mangum` (~136 MB); the scan Lambda's layer is unchanged |
| HTTP API `crypto-signals-dashboard` | two routes, one shared Lambda integration: `GET /api/health` (**open**, no authorizer) and, since Step 4.4, `ANY /api/{proxy+}` behind the Cognito JWT authorizer `DashboardJwtAuthorizer` (validates issuer, client id and expiry); `$default` stage, auto-deploy, throttle 10 rps / burst 20, no CORS |
| Cognito (Step 4.4) | user pool `crypto-signals-dashboard`: one admin-created user (no self-signup), email sign-in (case-insensitive), password ≥ 12 with lower + upper + digit + symbol, temp password valid 3 days, **TOTP MFA required** (no SMS; enrolled at first sign-in, §8.7), email-only recovery, LITE plan; **retained** on stack delete + deletion protection. Hosted UI prefix domain `crypto-signals-dash-<account-id>` (classic Hosted UI). Public app client `DashboardSpaClient`: authorization code + PKCE only, SRP, scopes `openid email profile`, callback `http://localhost:8765/callback` (the token helper; Step 4.5 adds CloudFront), access / ID tokens 1 h, refresh 30 d, token revocation on |
| Log groups | `DashboardApiLogs` (function) + `DashboardApiAccessLogs` (API access log), 14-day retention |
| IAM role | RDS Data API on the one cluster + read of its secret, CloudWatch Logs write; since Step 4.3 CloudWatch read (`DescribeAlarms` on `CryptoSignals-Monitoring-*`, `ListMetrics` + `GetMetricData` on `*`) — **no SSM, no S3, no Lambda** |
| Outputs | `DashboardApiUrl`, `DashboardFunctionName`; since Step 4.4 `DashboardUserPoolId`, `DashboardUserPoolClientId`, `DashboardCognitoDomain` (Hosted UI base URL `https://<prefix>.auth.<region>.amazoncognito.com`), `DashboardJwtIssuer` |

Since **Step 4.4** every `/api/*` route except `GET /api/health` — the Step 4.2 and
4.3 data routes (overview / setups / signals, market, funnel, analytics, Critic,
system health) and `/api/docs` / `/api/openapi.json` — is reachable through the API
URL **with** `Authorization: Bearer <access token>` (§8.6, login in §8.7). Without
a valid token API Gateway answers **401** before the Lambda runs (nothing is
invoked, the cluster is not woken). The health route does not open a DB call, so
browsing it never wakes the cluster; the Data API grant is what the data routes use.

The user pool is **retained**: deleting the stack (§8.5) leaves the pool (and its
user) in place, and a recreated stack creates a **new, empty** pool — re-create the
user there (§8.7) and delete the orphan by hand once deletion protection is turned
off, if wanted (it costs nothing). The same happens on a **template change that
forces user-pool replacement** (e.g. changing the sign-in attributes / username
configuration): under RETAIN CloudFormation creates a new **empty** pool (new pool
id, new client id, new issuer) and orphans the old one — re-create the user and
re-enrol TOTP (§8.7) and use the new outputs. cdk-nag `AwsSolutions-COG3`
(advanced security) and `-COG8` (Plus plan) are suppressed on the pool only, with
reasons in `dashboard_stack.py` (single operator; paid tiers); `-COG2` (MFA) is
satisfied — TOTP MFA is required (operator decision 2026-10-21).

### 8.2 Where the API URL is

CloudFormation console → `CryptoSignals-Dashboard` → **Outputs** → `DashboardApiUrl`
(and `DashboardFunctionName`); or the `cdk deploy` log of the Deploy (dev) run.
Record both in `docs/PROJECT_STATE.md §3` after the first deploy. Because this
stack deploys last, a red Deploy (dev) run can be Dashboard-only: if the run fails
on `CryptoSignals-Dashboard`, open that stack's CloudFormation events; the pipeline
stacks are already updated — do not roll them back.

### 8.3 Smoke test

```bash
curl -s https://<api-id>.execute-api.ap-south-1.amazonaws.com/api/health
# → {"status":"ok","version":"<short sha>"}
```

`version` is the deployed commit: `git rev-parse --short HEAD` of the synthesizing
checkout first (on the runner that is the tree `actions/checkout` fetched — on a
`workflow_run`-triggered deploy that is `head_sha`, whereas `GITHUB_SHA` is the
default-branch tip), then `GITHUB_SHA[:7]` if git is unavailable, else `local`. It
should match the merge commit that triggered the deploy — if it does not, the deploy
ran from a different commit than expected (`APP_VERSION` is baked at synth time).

The FastAPI app also defines `/api/openapi.json` and `/api/docs` (`openapi_url` /
`docs_url` in `create_app()`). Since **Step 4.4** they sit behind the JWT authorizer
like every other non-health route (design §6: "OpenAPI served at `/api/docs`
(behind the authorizer)"): `curl` with a token works, but a browser cannot send the
`Authorization` header, so `/api/docs` is **not browsable directly** until the SPA
(Step 4.5) — a plain browser visit gets API Gateway's 401 `{"message":"Unauthorized"}`.
Paths outside `/api/` still return API Gateway's `{"message":"Not Found"}`.

A request also produces one line in `DashboardApiAccessLogs` — since Step 4.2 a
JSON object per request (`routeKey`, `path`, `status`, … — fields in §8.6). Before
4.2 the stage used CDK's default Common Log Format, whose REST-era
`$context.resourcePath` an HTTP API logs as `-` (`"GET - HTTP/1.1" 200 35`).

### 8.4 Console editing

The function is zip-packaged like the scan Lambda (§4.1): **Code** tab shows
`src/dashboard/api/…`, Deploy, then call the URL. The same caveat applies — any
`cdk deploy` replaces console edits with the git contents. For `/api/health` use
the URL; for the data routes use the URL with a token (§8.7) or the console
**Test** event in §8.6 (which must carry authorizer claims since Step 4.4).

### 8.5 Removal

The stack is independent: deleting `CryptoSignals-Dashboard` in the CloudFormation
console removes the API, function, layer and log groups without touching the
pipeline stacks. The Cognito user pool is **retained** (RETAIN + deletion
protection, Step 4.4): it survives the delete, and the recreated stack makes a new
empty pool (§8.1) — re-create the user and re-enrol TOTP there (§8.7); a
template change that forces pool replacement has the same effect. No workflow exposes `cdk destroy` for it (the deploy workflow's
`recreate_stateless_stacks` covers Monitoring / Scheduling / Compute only), and the
hard rule rules out a local CLI — the console delete is the path. Note that the next
`cdk deploy --all` recreates it.

### 8.6 Data routes (behind the JWT authorizer since Step 4.4)

Step 4.2 added the first data routes to the FastAPI app (`src/dashboard/api/routers/`):

| Route | Returns |
|---|---|
| `GET /api/system/db` | `{"db": "ok" \| "resuming" \| "error", "latency_ms": …}` — the DB probe; **always 200** (it catches the Data API error itself because it *is* the probe) |
| `GET /api/overview` | health strip, risk budget (the §1.6 gates via `risk_gates`), open setups, today's scan windows (`src/common/schedule.py`), Forecaster coverage (`last_sweep_at`). Since Step 4.3 the strip carries the CloudWatch alarm states (`alarms`, 5 s budget; on failure `alarms: null` + `alarms_note: "cloudwatch unavailable"` or `"cloudwatch access denied"`) and each setup card a best-effort `unrealised_r` (below); the alarm and price fetches run concurrently with the DB reads |
| `GET /api/setups/open` | OPEN setups with signal summary + latest evaluation + best-effort `unrealised_r` (since Step 4.3) |
| `GET /api/signals?since&until&symbol&kind&outcome&tag&session&violated_rule&limit&cursor` | journal page with the derived decision kind (`classify.py`); `limit` 1..200 (default 50); `cursor` is the opaque keyset cursor from the previous page. **Rows are trimmed** (design §3.4, Data API 1 MB response cap): the `signals` columns *without* `payload` / `features` / `outcome_metadata`, plus the analyzer summary lifted out of the payload in SQL — `entry_price`, `stop_loss`, `take_profit_1`, `take_profit_2`, `risk_reward_ratio` (proposals) and `skip_reason`, `skip_details` (first 200 chars; skips) — and the derived `decision_kind`, `judge_ruling`, `session`, `has_setup`, `violated_rule`, `resolution_outcome`, `realized_r` |
| `GET /api/signals/{id}` | lifecycle: the **full** signal (`SignalDetailOut` = the journal row plus `payload` / `features` / `outcome_metadata` — the only place the payload travels), agent runs in order (trimmed per-step summaries), Forecaster evaluations, the Critic's resolution + review, setup card (no live price: `unrealised_r: null`, `unrealised_r_note: "not fetched on this view"`) |
| `GET /api/signals/{id}/runs/{run_id}` | one agent run's full output |

Step 4.3 added the market, funnel, analytics, Critic and system-health routes
(same app, same authorizer):

| Route | Returns |
|---|---|
| `GET /api/market/klines?symbol&tf=4h&limit=200` | Binance candles (`BinanceProvider` built **per request**, closed in `finally`; markets cached per container, 12 s bound — below). `symbol` must be in the dashboard's watchlist (`DashboardSettings.scan_symbols`) else 400 `unknown_symbol` before any provider is built; `tf` ∈ `Timeframe`; `limit` 1..500 |
| `GET /api/signals/{id}/chart?before=60&after=30` | H4 candles around the signal (`before` 1..500, `after` 0..500, window capped at now) + the chart overlay (entry/stop/TPs/POI for PUBLISHED/VETOED from the payload, GATE_REJECTED from the Analyzer run, `null` for NO_SETUP); 404 before any provider call |
| `GET /api/funnel?since&until&group_by=day\|window\|symbol` | decision funnel (scans → proposals → gate-passed → Judge rulings → published, rejections by rule) per group + `totals`; default 30 d, ≤ 365 d. `judge_*` incl. `judge_none` sum to `gate_passed`. `AD_HOC` console scans are their own group under `group_by=window`. `top_rejections_7d` / `_30d` are anchored to **now**, not `until` |
| `GET /api/analytics/win-rate?by=tag\|session\|strategy\|time&since&until` | published win rate per bucket; `win_rate: null` when 0 decisive (WIN/LOSS) |
| `GET /api/analytics/skeptic-value?since&until` | counted objections (`recommends_against` or `severity == "HIGH"`) by Judge ruling: `respected` / `overruled_with_caveat` / `overruled`; NoMacroData runs excluded (`no_macro_data_excluded`); a counted objection with no Judge ruling recorded is counted in `unruled` (no outcome) |
| `GET /api/analytics/equity-curve?since&until` | cumulative R for published, plus vetoed / gate-rejected counterfactuals **from replays only** |
| `GET /api/critic/reports?limit=10` | latest Critic reports (no markdown); `limit` 1..50 |
| `GET /api/critic/reports/{id}` | one report: full JSONB + markdown; 404 `not_found` |
| `GET /api/critic/reviews?limit=50&cursor` | Critic reviews newest first (keyset cursor, `error` trimmed to 500 chars); `limit` 1..200; 400 `bad_cursor` |
| `GET /api/system/scans` | the latest 50 scan runs with `window` and `duration_s` |
| `GET /api/system/agents?days=7` | per (UTC day, role) runs / errors / LLM cost + cost per day, plus the cost-free `scan` pseudo-role; `errors: null` = no failure source (below); `days` 1..30 |
| `GET /api/system/sweeps?days=7` | Forecaster evaluations / errors per scheduled sweep (empty sweeps listed as 0/0); `days` 1..30 |
| `GET /api/system/alarms` | the `CryptoSignals-Monitoring-*` alarm states; **always 200** (CloudWatch failure → `alarms: null` + note) |
| `GET /api/system/metrics?days=7` | the scan Lambda's daily invocations / errors / p95 duration (function discovered via `ListMetrics`); **always 200** (`note: "scan function not found"`, `"cloudwatch unavailable"` or `"cloudwatch access denied"`); `days` 1..30. **Fetch on page load / every 5–15 min, not on the 60 s poll** — `GetMetricData` is billed per metric (≈ $1.3/month at a 60 s cadence) |
| `GET /api/system/config` | config panel **inferred** from the DB (watchlist = symbols scanned in 7 d, model = latest `agent_runs` model, macro providers = any real objection in the last 10 Skeptic outputs, Langfuse = the dashboard's own `LANGFUSE_BASE_URL`); every value carries `"inferred": true` except the schedule (`SCAN_WINDOWS`) |

Analytics semantics (design §5.2a): default 90 d, ≤ 365 d (longer → 422, no SQL);
the effective outcome is the `decision_resolutions` replay when a row exists, else
the Forecaster's `signals.outcome`; every response carries
`source_counts: {"replayed": n, "forecaster": m}`.

**Cost / failure source rule** (`/api/system/agents`, `queries._USAGE_BY_DAY_SQL`,
`queries.UsageDay`):

- `agent_runs` is the source of runs and cost for **every** role, the Critic's
  successful reviews included. A row is written only on success, so it is **no
  failure source**: a (day, role) fed by `agent_runs` alone (analyzer, historian,
  skeptic, judge) reports **`errors: null`** — "unknown", never a claimed 0.
- `decision_reviews` adds only **failed** reviews (`error IS NOT NULL`, zero cost —
  never in `agent_runs`) as `critic` runs + errors. Reviews are upserted per
  signal, so **critic failures = signals whose latest review attempt failed**.
- `setup_evaluations` is the Forecaster's source (`ERROR` rows count as errors).
- `scan_runs` is the **`scan` pseudo-role**: runs = rows (one per symbol per
  invocation), errors = `FAILED` rows, cost 0 — this is where pipeline failures
  show. It is left out of `cost_per_day.by_role` (a scan-only day lists at $0).

**Long ranges are paged.** The funnel and outcome-row fetches read 365 days in
1,000-row ascending keyset pages (`queries.read_all_pages`, 20-page guard) so no
single Data API response approaches the 1 MB cap.

**Unrealised R** (overview + `/api/setups/open`): one latest-price fetch per
distinct OPEN-setup symbol, concurrent, 5 s overall budget. On timeout, a
provider error or any unexpected failure (logged with its traceback) the card
answers 200 with `unrealised_r: null` and `unrealised_r_note: "price unavailable"`
— Binance being slow never fails the page. The lifecycle page does not fetch a
price; its card says `"not fetched on this view"`.

**Market data cost + bound.** A fresh ccxt client would download Binance's full
`exchangeInfo` (spot + linear + inverse, several MB) before its first candle. The
dashboard's provider loads **linear (USDT-M) markets only** and keeps the loaded
table in a module global for the container's life (`market._MARKETS`, captured
after the first successful fetch), so only a cold container pays the market load.
The klines and chart routes are bounded by `market.MARKET_BUDGET_S` = **12 s**
(`asyncio.wait_for`); a blown budget is a `ProviderTimeoutError` → 502
`market_unavailable`, well inside the 25 s function timeout (never a gateway 5xx).
The scan pipeline's `BinanceProvider()` (no arguments) is unchanged.

**CloudWatch degradation.** `/api/system/alarms`, `/api/system/metrics` and the
overview alarm strip read CloudWatch with a bounded client (3 s connect, 10 s read,
one attempt). Any `ClientError` / `BotoCoreError` (throttling, no endpoint) — or
the overview's 5 s budget expiring — yields a `null` section with
`"cloudwatch unavailable"` (WARNING in the log) and HTTP **200**. An error code
starting `AccessDenied` is an IAM gap, not a hiccup: the section note is
**`"cloudwatch access denied"`** and the function log has it at **ERROR** — fix the
dashboard role grants below. IAM (Step 4.3): `cloudwatch:DescribeAlarms` on
`arn:aws:cloudwatch:<region>:<account>:alarm:CryptoSignals-Monitoring-*`;
`cloudwatch:ListMetrics` + `cloudwatch:GetMetricData` on `*` (these two have no
resource-level permissions — the `AwsSolutions-IAM5` suppression is justified in
`dashboard_stack.py`). Still no `ssm:`, `s3:` or `lambda:`.

**Routing and auth (Step 4.4).** All of these are served by `ANY /api/{proxy+}`
behind the Cognito JWT authorizer: call them through the API URL with
`Authorization: Bearer <access token>` (get one with the helper, §8.7). Without a
valid token API Gateway answers **401** `{"message":"Unauthorized"}` before the
Lambda runs — the data routes read Aurora, so an unauthenticated scanner can neither
read the journal nor keep the cluster awake. **Second layer:** under Lambda the app
itself (`src/dashboard/api/auth.py`, `AuthorizerClaimsMiddleware`) rejects every
request except `/api/health` whose event lacks `requestContext.authorizer.jwt.claims`
with a non-empty `sub` — **401** `{"reason":"unauthorized"}`, no route / DB / AWS
call. So a console Test event must carry claims (below), and an unknown or mistyped
path without claims answers 401, not 404. Outside Lambda (TestClient, local uvicorn)
the middleware is a no-op.

**Exercising one from the Lambda console.** Lambda → `DashboardApi…` function →
**Test** → create a new event (name it e.g. `api-overview`) with a minimal API
Gateway **v2** (HTTP API) payload — Mangum needs `version`, `rawPath`,
`rawQueryString`, `headers` and `requestContext.http`; the app's second layer needs
`requestContext.authorizer.jwt.claims` with a `sub` (any non-empty string — the
console bypasses API Gateway, so nothing validates it; the claims are what the
authorizer would have passed); the rest is filler:

```json
{
  "version": "2.0",
  "routeKey": "GET /api/overview",
  "rawPath": "/api/overview",
  "rawQueryString": "",
  "headers": {"host": "s2bc8yifa1.execute-api.ap-south-1.amazonaws.com"},
  "requestContext": {
    "http": {"method": "GET", "path": "/api/overview", "protocol": "HTTP/1.1",
             "sourceIp": "127.0.0.1", "userAgent": "console"},
    "authorizer": {"jwt": {"claims": {"sub": "console-test"}, "scopes": null}},
    "requestId": "console", "stage": "$default", "routeKey": "GET /api/overview",
    "accountId": "097853039368", "apiId": "s2bc8yifa1",
    "domainName": "s2bc8yifa1.execute-api.ap-south-1.amazonaws.com",
    "domainPrefix": "s2bc8yifa1", "time": "21/Oct/2026:00:00:00 +0000", "timeEpoch": 0
  },
  "isBase64Encoded": false
}
```

For another route change **all three** path copies together — `rawPath`,
`routeKey` and `requestContext.http.path`. Mangum's HTTP-API handler routes on
**`requestContext.http.path`**, not `rawPath` (the event above works because the
three agree; edit only `rawPath` and the old route still answers). The `routeKey`
values are illustrative only: a request proxied by API Gateway actually carries the
matched route, `ANY /api/{proxy+}`, and Mangum ignores `routeKey` — keeping them in
step with the path just makes the event self-describing. Query
parameters go in `rawQueryString` (e.g. `"limit=5&symbol=BTCUSDT"` — Mangum reads
that field, not `queryStringParameters`). The response is the Lambda proxy
shape: `{"statusCode": 200, "headers": {...}, "body": "<JSON string>",
"isBase64Encoded": false}`. Save the event — it survives in the console and is the
smoke test for any console edit of a data route (§8.4).

Two Step 4.3 examples, as edits of the event above (everything else unchanged):

- **Funnel by window** (`api-funnel`): `"rawPath": "/api/funnel"`,
  `"routeKey": "GET /api/funnel"`, `requestContext.routeKey` `"GET /api/funnel"`,
  `requestContext.http.path` `"/api/funnel"`, and
  `"rawQueryString": "group_by=window"` (add e.g.
  `&since=2026-10-20T00:00:00Z&until=2026-10-21T00:00:00Z` for one day).
- **Alarm strip** (`api-alarms`): `rawPath` / `requestContext.http.path`
  `"/api/system/alarms"`, both `routeKey`s `"GET /api/system/alarms"`,
  `"rawQueryString": ""`. Expect 200 with the five `CryptoSignals-Monitoring-*`
  alarms (display names without the prefix / CDK hash); `"alarms": null` with
  `"note": "cloudwatch unavailable"` means the CloudWatch call failed (the function
  log has the AWS error code); `"cloudwatch access denied"` means the role lacks the
  grant (check the `DescribeAlarms` resource scoping; ERROR in the log).
  This route does not touch Aurora, so it does not wake the cluster.

**Error bodies** (`src/dashboard/api/errors.py`, app-wide; uniform `{"reason": …}`):

| Status | Body | When |
|---|---|---|
| 401 | `{"reason":"unauthorized"}` | under Lambda, a request other than `/api/health` whose event has no JWT authorizer claims with a `sub` (console Test event without `authorizer`, a route wired without the authorizer, an unknown path); the route never runs. API Gateway's own rejection of a missing / invalid / expired token is a different body, `{"message":"Unauthorized"}`, and never reaches the Lambda |
| 503 + `Retry-After: 10` | `{"reason":"db_resuming"}` | Aurora is waking from scale-to-zero (`DatabaseResumingException`). The dashboard store is built with `resume_attempts=1` — **no retry**, the first resume error propagates at once (the scan Lambda's default budget is 6 attempts / ~75 s) — because an HTTP request behind API Gateway's 30 s cap cannot wait out the resume. |
| 502 | `{"reason":"db_error"}` | any other Data API `ClientError`; the AWS code + message are in the function log at WARNING, never in the body |
| 404 | `{"reason":"not_found"}` | `/api/signals/{id}` or `/runs/{run_id}` addresses a row that does not exist |
| 400 | `{"reason":"bad_cursor"}` | `cursor` is not one this deployment issued (also any cursor longer than 512 characters) |
| 400 | `{"reason":"unknown_symbol"}` | `/api/market/klines` for a symbol outside the dashboard watchlist (exact match — `btcusdt` is rejected); no provider is built |
| 502 | `{"reason":"market_unavailable"}` | `/api/market/klines` or `/api/signals/{id}/chart` when Binance fails (any `ProviderError`: down, rate-limited, timed out, malformed) or the 12 s market budget expires; the provider message is logged at WARNING, never echoed. The setup cards' unrealised R never raises this — it degrades to `null` instead |
| 422 | FastAPI validation body (`{"detail":[{"loc":…,"msg":…,"type":…}]}`) | request validation: `limit` outside its range (rejected, not clamped), a non-UUID `{id}` / `{run_id}`, an unknown `kind` / `outcome` / `group_by` / `by` / `tf` value, a malformed `since` / `until`, `days` outside 1..30 (`/api/system/agents`, `/sweeps`, `/metrics`) |
| 422 | `{"detail":"since must be before until"}` / `{"detail":"period exceeds 365 days"}` | funnel / analytics period checks (`period.py`) — a plain-string `detail`, not the validation list above; raised before any SQL |

The dashboard store's boto3 client is itself bounded (`deps.py`: 3 s connect, 15 s
read, one attempt) so the slowest Data API call fails inside the function's 25 s
timeout and one of the bodies above is returned — API Gateway's own 504 (30 s)
should never be what the UI sees.

**Expect the first call after idle to return 503 `db_resuming`** (Aurora takes
~10–30 s to resume); re-run the Test after ~10 s and it answers 200. `/api/system/db`
reports the same state as `{"db":"resuming"}` with a 200 instead. Every Test
invocation of a data route wakes the cluster (the usual scale-to-zero cost note
applies — do not poll it in a loop).

**Access-log fields.** Since Step 4.2 the `$default` stage writes one JSON object
per request to `DashboardApiAccessLogs` (`ACCESS_LOG_FORMAT` in
`infrastructure/stacks/dashboard_stack.py`): `requestId`, `ip`, `requestTime`,
`method`, `routeKey`, `path`, `status`, `responseLength`, `integrationLatency`,
`integrationError`. `routeKey` is the matched route (`GET /api/health`), `path`
the raw request path (populated even when no route matched); `integrationError`
is non-empty only when API Gateway, not the app, produced the 5xx. From Step 4.4 these
lines are also the only record of requests the JWT authorizer rejects before the
Lambda runs: such a line has `"routeKey":"ANY /api/{proxy+}"`, `"status":"401"` and
**no** `integrationLatency` (`"-"`), and the function log has nothing for it. Console Test invocations bypass the API and therefore write no access-log
line — only the function log.

### 8.7 Login and tokens (Step 4.4)

One operator, one Cognito user, no SPA yet (Step 4.5): tokens come from the local
PKCE helper `scripts/dashboard_token.py` (stdlib only; authorization code + PKCE
S256 through the classic Hosted UI; nothing is written to disk).

1. **Read the outputs.** After the Step 4.4 deploy: CloudFormation console →
   `CryptoSignals-Dashboard` → **Outputs** → `DashboardUserPoolId`,
   `DashboardUserPoolClientId`, `DashboardCognitoDomain` (plus `DashboardApiUrl`).
   Record them in `docs/PROJECT_STATE.md §3`.
2. **Create the user (once).** Cognito console → user pool `crypto-signals-dashboard`
   (the `DashboardUserPoolId`) → **Users** → **Create user**: email address as the
   username, **Send an email invitation**, set (or generate) a **temporary
   password** (≥ 12 chars, lower + upper + digit + symbol; valid 3 days). There is
   no self-signup — this is the only way in.
3. **Get a token** (from the repo root, port **8765** must be free — the app client's
   only registered callback is `http://localhost:8765/callback`, so do not change
   `--port` unless that URL is changed too):

   ```powershell
   .\.venv\Scripts\python.exe -m scripts.dashboard_token `
     --domain <DashboardCognitoDomain> --client-id <DashboardUserPoolClientId> `
     --call <DashboardApiUrl>/api/system/db
   ```

   It binds `127.0.0.1:8765`, opens the browser at the Hosted UI (the URL is also
   printed on stderr), and waits up to 120 s for the callback. **stdout is the access
   token only** (so `$token = .\.venv\Scripts\python.exe -m scripts.dashboard_token ...`
   works); with `--call` (must be `https://`) the HTTP status and the first 500
   characters of the body go to **stderr**. Errors are one `error:` line, exit
   non-zero, the token redacted. Only an **access** token works: the proxy route
   requires the `openid` scope and an ID token has no `scope` claim, so API Gateway
   rejects ID tokens with 401.
4. **TOTP MFA is required** (TOTP only, never SMS; operator decision 2026-10-21).
   The **first sign-in** through the helper goes: the Hosted UI forces the
   **password change** from the temporary password, then shows a **QR code** to
   enrol an authenticator app (TOTP), then asks for a **6-digit code** from that app
   to finish. Every later sign-in asks for the current code after the password.
   **Lost authenticator:** there is no self-service recovery of the second factor —
   an admin resets it in the Cognito console via the user's MFA settings (or resets
   the user's password / re-creates the user, step 2); the next sign-in then
   enrols a new authenticator.
5. **Call the API** (`<api>` = `DashboardApiUrl`; PowerShell, with `$token` from
   step 3 — `curl.exe`, not the `curl` alias):

   ```powershell
   curl.exe -s -i <api>/api/overview                                       # 401 {"message":"Unauthorized"} (no token; Lambda not invoked)
   curl.exe -s -i -H "Authorization: Bearer $token" <api>/api/overview     # 200 (first call after idle may be 503 db_resuming)
   curl.exe -s <api>/api/health                                            # 200 without a token
   ```

**Token validity.** Access and ID tokens last **1 h**, refresh tokens 30 d (the
helper uses only the access token; re-run it after an hour). The authorizer checks
the issuer (`DashboardJwtIssuer`), the client id and expiry; an expired token is a
401 from API Gateway. **Revocation window:** the JWT authorizer validates tokens
locally (signature, issuer, audience, expiry) and never asks Cognito whether a token
was revoked, so signing out or revoking the refresh token does **not** kill an
already-issued access token — it keeps working until it expires (≤ 1 h). To cut
access immediately, disable the user in the Cognito console (no new tokens, no
refresh) and wait out the hour. **Where 401s show up:** API Gateway's rejections only in
`DashboardApiAccessLogs` (`status` 401, no `integrationLatency`, §8.6); the app's
own `{"reason":"unauthorized"}` (missing claims) is a normal invocation in the
function log. `/api/docs` needs a token, so it is not browsable directly until the
SPA (Step 4.5).


### 8.8 Reading the dashboard banner (Step 4.6)

Open `https://dop305tg1xjkt.cloudfront.net/` and sign in. The red (or amber) banner at the
top of **Overview** appears only when something is wrong, and every banner links to
**Health** for the detail. What each line means and the first action:

| Banner | Meaning | First action |
|---|---|---|
| **Anthropic credit balance too low** (red) | The **latest finished** scan window FAILED and its stored `error_message` contains "credit balance" — the LLM call was rejected. | Top up Anthropic credits (Anthropic console → Billing). Once the next scan window succeeds the banner clears within one 60 s poll (earlier failed windows stay red on their own cards but no longer raise the banner). |
| **Latest scan failed (WINDOW)** (red) | The latest finished scan window FAILED and the error is *not* a credit rejection. | Health → Scan history: read the error (full text on hover); then the scan Lambda log group. |
| **N CloudWatch alarm(s) in ALARM** (red) | One of the five `CryptoSignals-Monitoring-*` alarms is firing; the names are listed. | Open the alarm in the CloudWatch console (§3). |
| **Forecaster needs attention** (amber) | Open setups not evaluated since the last sweep, or `ERROR` evaluations in the last window. | Health → Forecaster sweeps; usually the same credit cause as above. |
| **Alarm status unavailable** (amber) | The dashboard Lambda could not read CloudWatch (`cloudwatch unavailable` / `cloudwatch access denied`). Never treated as healthy. | Access denied = an IAM gap on the dashboard role — a bug; check the Lambda log. |

If a 60 s refresh fails, the page keeps the last data and shows an amber "Could not refresh … — showing the last data received" line instead of blanking. A scheduled window that has not fired yet shows **not run yet**; once 15 minutes late with
no scan it shows **missed** (amber). The credit wording depends on the failed scan's error
text reaching `scan_runs.error_message` (`scripts/run_scan.py` stores
`"<ExceptionType>: <message>"`); if a failure is swallowed before that, the banner falls
back to the generic or Forecaster wording. Lambda metrics on Health load once per page view
(not on the 60 s poll) because CloudWatch bills per request.


### 8.9 Reading the journal and a decision (Step 4.7)

**Signals** (`/signals`) is every decision the pipeline journaled, newest first — published
signals, Judge vetoes, risk-gate rejections and "no setup" skips. Filters (from / to date,
symbol, decision, outcome, session, tag, violated rule) live in the URL, so a filtered view can be
bookmarked or shared; text filters apply on Enter or when you leave the field. "Load more" pages
50 rows at a time. **Export CSV** writes the rows loaded so far (so load more first for older
rows); text cells that start with `=`, `+`, `-` or `@` are prefixed with `'` so a spreadsheet
does not run them as formulas.

Click a symbol to open the decision (`/signals/<id>`), the chain in order:

| Step | What it shows |
|---|---|
| Analyzer proposal / skip | The levels it proposed (or why it skipped). |
| Risk gate rejected | The SPEC §1.6 hard rule that blocked it, and the details. Only the first violated rule is stored. |
| Historian | Similar past setups and their win rate; each match links to that past decision. |
| Skeptic | The strongest objection, or "Macro data unavailable" (amber) when macro providers had no data. |
| Judge | PUBLISH / PUBLISH_WITH_CAVEAT / SKIP with confidence and caveat. |
| Forecaster | Each look at the open setup (still valid / at risk / closed); an `ERROR` row (red) carries the failure text — usually the credit balance. |
| Outcome (replay) | The deterministic 5m-candle replay result and realised R (Critic v0). For a decision that was **never published** (a veto or gate rejection) it reads **Outcome (counterfactual replay) … not taken** in neutral grey — what would have happened, not a trade result. `UNRESOLVABLE` means no replay was possible (no R is shown). When no replay exists yet, a plain **Outcome** step shows the Forecaster's recorded result. |
| Critic review | The Critic's lesson, or "Review failed" (amber). |

Each agent step has a **Raw output** button: the full stored output is fetched only when you open
it. **Signal JSON** shows the stored payload and features. The H4 candlestick chart arrives with
the Markets page (Step 4.9).
