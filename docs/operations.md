# Operations Runbook

> Day-2 operations for the multi-agent crypto-signals system (Step 2.12).
> Architecture and live resource IDs live in [`PROJECT_STATE.md`](PROJECT_STATE.md);
> this is the *how-to* for secrets, deploys, alarms, and incidents.

**Live facts** (full table in `PROJECT_STATE.md §3`):

| | |
|---|---|
| Account / Region | `097853039368` / `ap-south-1` (Mumbai — pinned in `infrastructure/app.py`) |
| Scan Lambda | `CryptoSignals-Compute-ScanLambdaDD7505A5-rfFwKEhPAnlo` |
| Scan log group | `CryptoSignals-Compute-ScanLambdaLogs7DF29218-YGmufZcOKKgE` |
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
approval gate). To deploy by hand from the repo root (Docker Desktop must be
running — the Lambda image builds locally):

```bash
cd infrastructure
cdk deploy --all --region ap-south-1
```

Stacks (dependency order): `Network → Data → Compute → Scheduling → Monitoring`.
First-time-on-this-account only: `cdk bootstrap` (already done for ap-south-1).

> If switching off Secrets Manager for the first time, **create the SSM
> parameters (§1.1) before the deploy** so the post-deploy scan can read them.

---

## 3. Alarms (NFR-2.2)

CloudWatch alarms publish to an SNS topic, which a small **notifier Lambda**
(`AlarmNotifier`, reuses the scan image) forwards to the operator's Telegram bot.
No manual SNS subscription is needed — CDK wires it.

| Alarm | Fires when | Source |
|---|---|---|
| `ScanFailureRateAlarm` | scan Lambda error rate > 10% over 24h | Lambda Errors/Invocations |
| `ScanLatencyP95Alarm` | scan p95 duration > 2 min | Lambda Duration p95 |
| `AuroraCpuAlarm` | Aurora CPU > 80% for 15 min | RDS CPUUtilization |
| `ProviderErrorAlarm` | ≥ 3 provider errors in 1h | Logs metric filter on `PROVIDER_ERROR` |

**Low-volume note:** at ~5 scans/day the failure-rate and provider-error alarms are
effectively "any failure/several provider errors today" — intended sensitivity for
a system the operator trades from. Richer provider-error tracking lands in Step 2.13.

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
SSM standard parameters and the SNS/alarm path are free. Anthropic spend tracking
+ a budget alarm land with the Critic in Slice 3 (NFR-5.1/5.2). Watch the AWS
Billing console; investigate if the monthly run-rate exceeds ~$10.
