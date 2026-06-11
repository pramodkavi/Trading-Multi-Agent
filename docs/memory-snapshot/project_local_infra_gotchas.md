---
name: project-local-infra-gotchas
description: "Two Windows-specific local-infra gotchas resolved at Step 1.12 (Postgres port, aiodns)"
metadata:
  node_type: memory
  type: project
  originSessionId: f29f3cf9-20ea-45e3-bf96-82b625abdeba
---

Two environment gotchas hit + fixed during the Step 1.12 live end-to-end run on the user's Windows machine:

**1. Native PostgreSQL 15 shadows the Docker container on port 5432.**
The user has a native `postgresql-x64-15` Windows service listening on `0.0.0.0:5432` (IPv4). It shadows the Dockerized `pgvector/pgvector:pg16` container, so `localhost:5432` from the host hits native PG-15 (no `signals` user → "password authentication failed"). Fix applied: `docker-compose.yml` now maps host port **5433**→5432. DATABASE_URL must use `postgresql://signals:signals@localhost:5433/signals`. The container still listens on 5432 internally.

**How to apply:** Always use port 5433 for the dev DB on this machine. The `.env` DATABASE_URL is set to 5433. If a fresh `docker compose up -d db` shows the container, connect via 5433.

**2. aiodns (CCXT's async resolver) fails on this Windows box.**
CCXT's aiohttp uses aiodns/c-ares by default, which raises "Could not contact DNS servers" here even though the OS resolver + httpx work fine. Fix applied in `src/providers/binance.py`: when BinanceProvider builds its own ccxt client it attaches an aiohttp session with `aiohttp.ThreadedResolver()`. Portable, negligible cost. Mocked tests inject a client so they never hit this path.

**How to apply:** If a future provider (FRED, Twelve Data at Step 2.3) uses aiohttp/ccxt and DNS-fails on Windows, apply the same ThreadedResolver pattern.

**3. CDK toolchain (Step 1.14+).** aws-cdk CLI installed globally via npm (2.1125.0); aws-cdk-lib/constructs/cdk-nag in `.venv` (see `infrastructure/requirements.txt`). The machine runs **Node v25.6.0**, newer than CDK's tested set (<=24) — jsii prints a noisy untested-version warning but synth works. Silence it with `$env:JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION = "1"`.

**CDK synth recipe** (no AWS account needed; runs locally):
```
$env:PATH = "$(Join-Path (Get-Location) '.venv\Scripts');$env:PATH"  # so `python app.py` finds aws-cdk-lib
$env:JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION = "1"
cd infrastructure
cdk ls           # lists CryptoSignals-{Network,Data,Compute,Scheduling,Monitoring}
cdk synth        # writes CloudFormation to cdk.out/ (gitignored)
```
CDK app is hand-authored (not `cdk init`): `infrastructure/app.py` + `cdk.json` (app=`python app.py`) + `stacks/*_stack.py`. mypy is intentionally scoped to src/+scripts only; infrastructure/ is NOT type-checked (CDK + mypy --strict is high-friction). ruff DOES lint infrastructure/. Steps 1.15-1.18 fill the empty stacks; 1.15+ needs real AWS creds + `cdk bootstrap` + deploy.

**Live-run recipe** (Docker must be up):
```
docker compose up -d db
$env:DATABASE_URL = "postgresql://signals:signals@localhost:5433/signals"
.\.venv\Scripts\python.exe scripts\migrate.py        # once per fresh volume
.\.venv\Scripts\python.exe scripts\run_scan.py --symbol BTCUSDT
```

See [[project-precommit-friction]] for the other recurring frictions.
