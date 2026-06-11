# Memory Index

- [User collaboration style](user_collaboration_style.md) — gradual bundles, confirm scope first, report stop point
- [Project: policy vs schema split](project_policy_vs_schema.md) — why SignalProposal schema bounds are looser than SPEC §1.6 policy
- [Project: Step 1.5 resolution](project_step_1_5_blocker.md) — RESOLVED: implemented from scratch; analyzer is Slice 1 stub, expands to full SMC in Slice 2 Step 2.1
- [Project: pre-commit friction](project_precommit_friction.md) — new src/ dep → update pyproject + mypy hook deps; test_migrate.py reformats every commit (ruff version skew), re-add + retry
- [Project: local infra gotchas](project_local_infra_gotchas.md) — Docker DB on port 5433 (native PG-15 shadows 5432); aiodns broken on Windows, BinanceProvider uses ThreadedResolver; live-run recipe
- [Project: serverless pivot](project_serverless_pivot.md) — deployment moved Fargate→Lambda+Aurora Data API (SPEC revised, commit 17ab10c); dual-backend repos; Steps 1.18-1.21 complete; Step 1.22 paused for cost review
- [Architecture: DynamoDB vs Aurora](architecture_dynamodb_vs_aurora.md) — cost/feasibility analysis; Aurora Serverless v2 now ($2-5/mo Slice 1), DynamoDB revisit in Slice 2 if costs exceed $10/mo (20h refactor effort)
