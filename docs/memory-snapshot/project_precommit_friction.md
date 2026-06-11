---
name: project-precommit-friction
description: Two recurring pre-commit frictions in the trading-system repo and how to handle them
metadata:
  node_type: memory
  type: project
  originSessionId: f29f3cf9-20ea-45e3-bf96-82b625abdeba
---

Two friction points recur on almost every commit in this repo (E:\AI\Trading Multi Agent):

**1. New runtime dep imported in src/ → must update TWO places.**
Adding a third-party package that src/ imports requires both:
- `pyproject.toml` `dependencies` (runtime + local mypy)
- `.pre-commit-config.yaml` mypy hook `additional_dependencies` (the hook runs in an isolated venv)
Hit for: anthropic (1.6), langgraph (1.7), psycopg (1.8), asyncpg (1.9), pydantic-settings (1.11). Preempt it whenever adding a dep.

**Why:** the pre-commit mypy hook can't resolve imports it doesn't have installed in its own env; a missing one fails the commit with "Cannot find implementation or library stub for module".

**How to apply:** when adding a dep, edit both files in the same change before committing.

**2. `tests/unit/test_migrate.py` gets reformatted by the ruff-format pre-commit hook on nearly every commit**, even though local `ruff format --check .` reports it clean (Steps 1.8, 1.9, 1.10, 1.11). Root cause: a ruff version skew between the local venv (ruff 0.15.x) and the pinned `astral-sh/ruff-pre-commit rev: v0.6.9` in .pre-commit-config.yaml. The hook auto-fixes, fails the first commit attempt, then a re-`git add` + re-commit succeeds.

**How to apply:** when a commit fails on ruff-format with "files were modified by this hook", just `git add` the reformatted file(s) and re-run the same commit — it passes on the second try. To fix permanently, bump `rev: v0.6.9` in .pre-commit-config.yaml to match the installed ruff version (~0.15) and align the mirrors-mypy rev too.

See [[user-collaboration-style]].
