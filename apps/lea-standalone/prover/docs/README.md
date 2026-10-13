# Lea docs

Project docs and learnings kept alongside the code. This folder is where design
rationale, architecture decisions, and notes-for-later live, so the *why* behind
the code survives beyond any single change.

## Index

- [design.html](design.html) — **interactive architecture diagram** (open in a
  browser): color-coded config-in / event-out flow + turn lifecycle.
- [design.md](design.md) — architecture overview in prose: component
  responsibilities, the turn lifecycle, event contract, config schema, and
  extension points (links to design.html for the visuals).
- [decisions.md](decisions.md) — architecture decision log: the config-driven
  direction, agent-as-product / eval-as-consumer split, the streaming LiteLLM
  engine, `model_kwargs`, cost transparency, and the mini-swe-agent alignment —
  with rationale.
- [lean-lsp-mcp.md](lean-lsp-mcp.md) — **how-to guide**: wire the `lean-lsp-mcp`
  server (Lean LSP tools + Mathlib search) and a Lean skill into Lea via config,
  with a full copy-paste example.

## Conventions

- One concern per file; link between files rather than duplicating.
- Decisions are append-mostly: when one is reversed, add a new dated entry that
  supersedes it rather than silently editing history.
