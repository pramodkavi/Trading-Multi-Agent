# Memory Snapshot

These files are a **verbatim copy of the AI assistant's (Claude Code's) working memory**
from the machine where Slice 1 was built. That memory normally lives *outside* the repo at
`~/.claude/projects/<project>/memory/` and is **not** included in a `git clone`, so it was
copied here to travel with the repository to a new machine / different Claude subscription.

## What's here

| File | Type | What it captures |
|---|---|---|
| `MEMORY.md` | index | One-line pointer to each memory below. |
| `project_serverless_pivot.md` | project | **The big one.** Full log of the Fargate→Lambda+Aurora pivot and every Slice-1 step (1.13→1.22): commits, decisions, gotchas, live resource IDs. |
| `architecture_dynamodb_vs_aurora.md` | project | DynamoDB-vs-Aurora cost/feasibility analysis (deferred to Slice 2). |
| `project_local_infra_gotchas.md` | project | Windows infra gotchas (PG port 5433, aiodns, CDK/Node, run recipes). |
| `project_precommit_friction.md` | project | Recurring pre-commit frictions and how to clear them. |
| `project_policy_vs_schema.md` | project | Why `SignalProposal` schema bounds are looser than SPEC §1.6 policy. |
| `project_step_1_5_blocker.md` | project | How the missing `detect_structure.py` was handled (implement from scratch); applies again at Slice 2 Step 2.1. |
| `user_collaboration_style.md` | user | How the user wants to collaborate (one step/session, confirm scope, report stop point). |

## For Claude on the new machine

1. **Read all of these files** at the start of your first session (after `CLAUDE.md` and
   `docs/PROJECT_STATE.md`).
2. **Re-seed your own memory** from them: create equivalent entries in your local memory
   directory so they persist across *your* sessions too. Keep the same names/structure.
3. These are **point-in-time notes** — some `file:line` references or "current status" lines
   may be stale. Verify against the actual code/`git log` before asserting any claim as fact.
   `docs/PROJECT_STATE.md` is the most up-to-date summary; prefer it where they disagree.

## For the human

You don't need to read these unless you're curious about *why* something was built a certain
way. The operational summary you want is `docs/PROJECT_STATE.md`.
