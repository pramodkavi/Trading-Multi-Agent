---
name: user-collaboration-style
description: How the user wants to collaborate on multi-step implementation work
metadata:
  node_type: memory
  type: user
  originSessionId: f29f3cf9-20ea-45e3-bf96-82b625abdeba
---

User prefers gradual, bundled implementation when working through long roadmaps (like SPEC.md's ~70 steps). Concrete pattern observed in Slice 1 session:

- Asks for upfront scope estimate ("decide upto which step you can develop")
- Wants confirmation of understanding before any code is written
- Explicitly does not want too much in one iteration
- Wants the agent to report which step it stopped at when pausing

Right size for a bundle: foundational steps that lock together (e.g., scaffolding → tooling → first models with tests) so the bundle ends at a state where checkpoints actually mean something. Stopping mid-bundle, e.g., right after tooling install with no code yet, isn't useful — but bundling beyond the first meaningful milestone risks context bloat.

When in doubt about scope, use AskUserQuestion with 3 options framed as "recommended / safer / more ambitious" so the user can redirect.
