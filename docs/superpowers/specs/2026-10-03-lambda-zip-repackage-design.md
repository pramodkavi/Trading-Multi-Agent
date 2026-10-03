# Lambda Zip Repackage (console-editable code) — Design

> **Status:** approved in design review 2026-10-03; implementation plan follows.
> **Changes:** SPEC §2.4 (Compute Layer packaging), §2.6 (container scanning row),
> Step 1.18 / 1.20 wording. The SPEC amendments in §9 ship in the same PR.
> **Authority:** `SPEC.md` remains authoritative. Where this design deviates from it,
> the deviation is listed in §9 and must be written into SPEC in the same commit.

---

## 1. Goal

Make the deployed application **readable and editable in the AWS Lambda console**:
open the function, see every `.py` / `.yaml` file, change a prompt, a threshold, a
line of pipeline logic, or add a log statement, click Deploy, click Test, read the
result. No local build, no Docker, no `cdk deploy` for an experiment.

Operating constraints agreed in the review:

1. **One environment.** Dev, test and prod are the same account/region
   (`097853039368` / `ap-south-1`). No sandbox function, no promotion flow.
2. **Console edits are not synced back to git.** The operator accepts that the next
   `cdk deploy` (manual, or the auto-deploy on push to `main`) replaces the function
   code with the repository contents. This is documented, not prevented.
3. **Cost must not get worse.** Current all-in estimate is $5–9/month
   (`docs/PROJECT_STATE.md`). The repackage is cost-neutral: same memory, timeout,
   invocation count; zip + layer storage is free at this size.
4. **The LLM layer does not change.** Anthropic API direct, `src/common/llm.py`,
   model pin, retries — untouched. See §8 for why Bedrock / AgentCore were rejected.

## 2. What exists today and why it blocks the goal

| Fact | Consequence |
|---|---|
| Scan Lambda is `lambda_.DockerImageFunction` built from `Dockerfile.lambda` (`infrastructure/stacks/compute_stack.py:94`). | Console shows an image URI only. No source, no editor, no Test-tab iteration on changed code. |
| The alarm-notifier Lambda in `MonitoringStack` reuses the **same image** with the CMD overridden (`monitoring_stack.py:134-141`). | Deleting the Dockerfile means the notifier must be repackaged too. |
| Lambda console inline editor requires a **zip deployment package under 3 MB**. Function + layers must stay **under 250 MB unzipped**. | Source and dependencies must be split: tiny code zip + one dependency layer. |
| Measured 2026-10-03 (pip `--platform manylinux2014_x86_64 --only-binary=:all:`, py3.11): all runtime deps = **211 MB** unzipped; `src/` + `scripts/` = **0.58 MB**. ccxt alone is 63 MB. | Fits. Margin is thin, so a size guard is part of the design. |
| `psycopg[binary]` (19 MB) is a base dependency but is imported **only** by `scripts/migrate.py` (psycopg backend) and integration tests; nothing under `src/` imports it. `boto3` is pre-installed in the Lambda runtime. | Both are excluded from the layer. Layer lands at **~190 MB**. |
| `asyncpg` is imported at module top-level in `src/persistence/store.py:44` even though the Lambda uses the Data API backend. | asyncpg (10.5 MB) **stays in the layer**; changing the import is out of scope. |
| `ComputeStack` exports the function ARN; `SchedulingStack` and `MonitoringStack` import it (`infrastructure/app.py:65-72`). | CloudFormation cannot change `PackageType` Image→Zip in place: the function is **replaced**, its ARN changes, and CloudFormation refuses to modify an exported value that is still imported. Drives the cutover in §6. |
| CI job `image` builds the image and runs Trivy on push to `main` (`.github/workflows/ci.yml:72-94`). SPEC §2.6 lists "Container scanning: Trivy". | Replace with a dependency scan so the CVE gate survives without an image. |
| `deploy-dev.yml` / `deploy-prod.yml` run `cdk deploy --all`; CDK builds the image with Docker on the runner. | After this change no Docker is needed on the deploy runner or on the operator's laptop. Workflow steps are unchanged; comments are. |

## 3. Design

### 3.1 Function packaging (scan Lambda)

- `lambda_.Function` with `runtime=PYTHON_3_11`, `architecture=X86_64`,
  `handler="scripts.run_scan.lambda_handler"` (unchanged string).
- `code=lambda_.Code.from_asset(REPO_ROOT, exclude=[...])` where the exclude list keeps
  **only** `src/**` and `scripts/**` and drops `__pycache__`, `*.pyc`, tests, docs,
  infrastructure, `.git`, etc. The zip root therefore contains `src/` and `scripts/`
  exactly as the repo does, so `import src.…` and `scripts.run_scan` resolve without a
  `pip install`. `src/persistence/*.sql` and `src/config/strategies.yaml` are included
  because they live inside those directories.
- `layers=[deps_layer]` (§3.2). Memory 1024 MB, timeout 10 min, environment
  variables, IAM grants, explicit log group, description: **unchanged**.
- The function keeps construct ID `ScanLambda`. Because the resource is replaced anyway
  (§6), no logical-ID trick is needed.

### 3.2 Dependency layer

- `lambda_.LayerVersion` with `compatible_runtimes=[PYTHON_3_11]`,
  `compatible_architectures=[X86_64]`, `code=lambda_.Code.from_asset(...)` with
  **local bundling** (no Docker). The bundling command is a repo script,
  `infrastructure/build_layer.py`, which:
  1. Reads `[project].dependencies` from `pyproject.toml` (single source of truth; no
     second requirements file to drift).
  2. Removes an explicit exclude set `{"boto3", "psycopg"}` with a comment giving the
     reason for each (boto3 → in the runtime; psycopg → local migrate path only).
  3. Runs `pip install --target <out>/python --platform manylinux2014_x86_64
     --only-binary=:all: --python-version 3.11 --implementation cp <deps>`.
  4. Strips `__pycache__` directories only. Package metadata and any `tests/`
     directories inside dependencies are left alone, so the layer contents stay
     predictable and match a plain `pip install`.
  5. **Size guard:** fails with a clear message if the unzipped layer exceeds 240 MB
     (10 MB headroom under the 250 MB hard cap) or if the code asset exceeds 2.5 MB
     (headroom under the 3 MB console-editor cap).
- The asset hash is computed from the **bundling output**, so the layer is rebuilt and
  re-uploaded only when the dependency set actually changes. Code-only changes deploy
  just the 0.6 MB zip, which makes `cdk deploy` fast and makes
  `cdk deploy --hotswap CryptoSignals-Compute` a seconds-long operation.
- Docker bundling is **not** configured as a fallback. pip `--platform` resolution works
  on Windows and ubuntu runners (verified 2026-10-03). If a future dependency has no
  manylinux wheel, the build fails loudly and the decision is revisited.

### 3.3 Alarm-notifier Lambda (MonitoringStack)

- Becomes a `lambda_.Function` with `handler="scripts.alarm_notifier.lambda_handler"`,
  **the same code asset and the same layer** as the scan Lambda. The notifier imports
  `src.config.secrets` and `src.notifications` (httpx), so it needs the layer; sharing
  one asset keeps "one artifact to build and patch", the property the image gave.
- `ComputeStack` exposes `self.code_asset` and `self.deps_layer`; `MonitoringStack`
  receives them as constructor arguments (a new cross-stack reference, acceptable since
  Monitoring is recreated in the cutover anyway).
- Memory/timeout/env/IAM/log group: unchanged.

### 3.4 Console workflow (what the operator does)

1. Lambda console → the scan function → **Code** tab. The editor shows the `src/` and
   `scripts/` tree. Edit any file.
2. **Deploy** (console button). Seconds.
3. **Test** tab, saved events:
   - `{}` — full watchlist scan
   - `{"symbols": ["BTCUSDT"]}` — one symbol (`scripts/run_scan.py:646`)
   - `{"mode": "forecaster"}` — Forecaster sweep
   - `{"mode": "resolve", "chunk_size": 25}` — Critic v0 outcome resolver
   - `{"mode": "migrate"}` — schema apply (escape hatch)
   The returned JSON summary appears inline; logs under **Monitor → View CloudWatch logs**.
4. **Caveat (documented in `docs/operations.md`):** any `cdk deploy`, including the
   automatic dev deploy after CI on `main`, replaces the code with the git contents.
   Console edits the operator wants to keep must be copied into the repo by hand.

### 3.5 Removals

- Delete `Dockerfile.lambda`. The root `Dockerfile` and `docker-compose.yml` stay (local
  one-shot run). `.dockerignore` stays for the root Dockerfile.
- Delete the CI `image` job. Add a `deps-scan` job (push to `main` only, as before)
  that runs `python infrastructure/build_layer.py --requirements-only > req.txt` and
  `aquasecurity/trivy-action` in `fs` mode against it, `severity: CRITICAL,HIGH`,
  `ignore-unfixed: true`, `exit-code: 1`. Same gate semantics, no image.
- The setuptools/wheel CVE workaround in the Dockerfile is no longer needed: the layer
  does not ship setuptools unless a dependency requires it.

### 3.6 cdk-nag

- `AwsSolutions-L1` (non-latest runtime) will fire for `python3.11` on both functions.
  Add a resource-level suppression with reason: *"Project toolchain, tests and wheels
  are pinned to 3.11 (pyproject `requires-python`, CI matrix); runtime upgrade is a
  deliberate separate step."* Existing IAM4/IAM5 suppressions carry over unchanged.

## 4. Error handling

| Failure | Behaviour |
|---|---|
| Layer > 240 MB or code zip > 2.5 MB | `cdk synth` fails in `build_layer.py` with the measured sizes and the offending top-5 packages. |
| A dependency has no manylinux wheel | pip fails under `--only-binary=:all:`; synth fails; message names the package. |
| Console edit introduces a syntax error | Lambda init fails on next invoke; the existing failure-rate alarm → Telegram path fires (MonitoringStack). The operator fixes it in the console or redeploys from git. |
| Operator forgets a console edit and CI deploys | Edit is silently lost. Accepted (§1 constraint 2); documented. |

## 5. Testing

- **CDK assertions (`tests/infra/test_stacks.py`):**
  - `compute`: exactly one `AWS::Lambda::Function`; `Runtime: python3.11`;
    `Handler: scripts.run_scan.lambda_handler`; `PackageType` absent or `Zip`;
    exactly one `AWS::Lambda::LayerVersion`; function `Layers` references it.
  - `monitoring`: the notifier function is `Zip` with
    `Handler: scripts.alarm_notifier.lambda_handler` and the same layer reference.
    Existing alarm/SNS/metric-filter tests unchanged.
- **Unit tests for `infrastructure/build_layer.py`:** dependency list parsing from a
  temp `pyproject.toml`; exclusion of `boto3` and `psycopg` (and `psycopg[binary]`
  extras syntax); size-guard raises on an over-limit directory (use a fake size walker,
  no real pip).
- **Synth smoke:** the existing `templates` fixture already synthesises every stack, so
  the layer bundling runs in the test suite. To keep `pytest` fast and offline, the
  fixture sets the CDK context flag `aws:cdk:bundling-stacks` to `[]` (skip bundling)
  — the bundling path itself is exercised by `cdk synth` in CI (`quality` job).
- **Manual post-deploy checkpoint (SPEC §5.3 style):** after cutover, run the five Test
  events from §3.4 in the console; confirm a Telegram message for the scan and a log
  line for each mode; confirm the alarm notifier by publishing a test message to the
  SNS topic.
- Universal checkpoints (ruff, mypy --strict, pytest, pre-commit, no secrets) apply.

## 6. Cutover (one-time, ~10 minutes)

Because the `PackageType` change forces a replacement and the ARN is a consumed
export, the three **stateless** stacks are destroyed and recreated. The Data stack
(Aurora, S3, DB secret) and the SSM parameters are **not touched**.

```
cd infrastructure
cdk destroy CryptoSignals-Monitoring CryptoSignals-Scheduling CryptoSignals-Compute
cdk deploy --all --require-approval never
```

Effects to expect, all accepted:

- Scan and notifier log groups are deleted (`RemovalPolicy.DESTROY`); history under the
  2-week retention is lost.
- EventBridge schedules are recreated with new names; `ResolverScheduleName` output
  changes. `critic.yml` reads nothing by schedule name, so no Actions change.
- The GitHub OIDC deploy role needs no new permissions beyond what `cdk deploy --all`
  already uses (Lambda, Layers, S3 asset bucket). No ECR permissions are needed anymore;
  they are left in place.
- `docs/PROJECT_STATE.md` live-resource IDs (function name/ARN, log group) are updated
  after the deploy.

Post-cutover, `deploy-dev.yml`'s transitional "Compute-first" step keeps working (it is
harmless) and can be removed in the same PR since the 2.12 export transition is complete
on the recreated stacks.

## 7. Documentation changes (same commit as the code)

- `SPEC.md` §2.4 table rows "Agent compute" and "Packaging"; §2.6 "Container scanning"
  row → "Dependency scanning (Trivy fs)"; Step 1.18 and 1.20 bullets.
- `CLAUDE.md` §4 Compute row: "Lambda (zip + dependency layer, console-editable)".
- `docs/PROJECT_STATE.md`: deployment description, live IDs, gotcha "console edits are
  overwritten by cdk deploy".
- `docs/operations.md`: new section "Editing and testing in the Lambda console" with the
  §3.4 steps and events; the layer size-guard failure and how to read it.
- `docs/memory-snapshot/project_serverless_pivot.md`: one line noting the repackage.

## 8. Rejected alternatives (recorded so they are not re-litigated)

| Option | Why not |
|---|---|
| **Amazon Bedrock AgentCore Runtime** as the host | Deploys container images / code bundles with no inline editor, so it does not solve the visibility problem. Priced per vCPU-hour ($0.0895) and GB-hour ($0.00945, V1 in ap-south-1) with memory billed through a 15-minute idle window after each run; for a 4×/day batch job this is at best cost-equal to Lambda. V2 runtime is not available in ap-south-1. Session/endpoint model is built for conversational agents, not cron batch. |
| **Claude on Bedrock** instead of the Anthropic API | Same per-token price for Sonnet 4.5 ($3 / $15 per MTok). In ap-south-1 current Claude models are available only via global cross-region inference. No Batches API, no Models API. Zero cost benefit; adds an SDK client swap and IAM work. |
| **Claude Platform on AWS** (Anthropic-operated, SigV4, AWS billing) | Viable later if one AWS bill / no API key in SSM is wanted. Deferred: not a cost lever, and the operator chose to leave the LLM layer alone. |
| Keep the container and add a second **sandbox zip function** | Operator wants a single function; environment separation explicitly out of scope. |
| **Read-only console + `cdk deploy --hotswap`** | Operator wants to edit in the console, not just read. (Hotswap remains available as a bonus of zip packaging.) |
| Python 3.12/3.13 runtime to satisfy `AwsSolutions-L1` | Toolchain is pinned and tested on 3.11; a runtime bump is its own step. Suppression with reason instead. |

## 9. SPEC deviations to write in the same PR

1. §2.4 "Packaging: Container image in Amazon ECR (deps too large for a zip)" →
   "Zip deployment package (`src/` + `scripts/`, <3 MB, console-editable) + one
   dependency Lambda Layer (~190 MB unzipped, built by `infrastructure/build_layer.py`
   without Docker)". The rationale "deps too large for a zip" was true for a single zip
   and is false with a layer; record the measured sizes.
2. §2.6 "Container scanning: Trivy" → "Dependency scanning: Trivy `fs` over the layer
   requirement set".
3. Step 1.18 / 1.20: "build the Lambda container image" → "synthesise the zip + layer
   assets".
4. New operational note under §6.1 or `operations.md`: console editing is a supported
   workflow for this single-environment deployment; git remains the source of truth for
   `cdk deploy`.

## 10. Out of scope

- Any change to `src/common/llm.py`, model choice, prompt caching, or Haiku tiering.
- Langfuse deployment or per-scan token/cost persistence (separate observability step).
- Fixing the top-level `asyncpg` import in `store.py` to shrink the layer further.
- Multi-environment / sandbox functions; syncing console edits back to git.
- Dashboard (Slice 4) packaging.
