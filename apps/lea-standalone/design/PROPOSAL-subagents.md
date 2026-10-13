# Extending Lea with Subagents — Design Proposal

**Status:** draft for review · **Date:** 2026-07-16 · **Author:** Shaswat Patel
**Scope:** why Lea should move from one serial proving loop to a coordinator + subagents,
what we borrow from two existing agent harnesses, and what it costs.
**Companion docs:** `v2.3-concurrent-runs-architecture.md` (the full technical design,
decisions D69–D84) · `architecture-figure-d2.html` (current backend architecture).

---

## 1. Summary

Lea today runs **exactly one proving loop at a time**, and that loop explores proof
strategies **serially inside a single conversation**. We propose two linked changes:

1. **Concurrency substrate** — allow N proof runs at once (N sessions, or one session's
   subagents), by fixing the shared state that currently makes concurrency unsafe.
2. **Subagents** — a coordinator agent that delegates premise search, proof attempts, and
   diagnostics to scoped child agents working in parallel on isolated files, then promotes
   one clean proof.

The design is **not speculative**: both mechanisms are lifted from two production coding
agents whose source we audited (opencode, OpenHands), and specialized for Lean.

**The finding that motivates urgency:** the single-run restriction is not a product
decision — it is a mutex hiding real concurrency bugs. Removing it naively does not give
concurrency; it gives **silently wrong proof verdicts** (§4.2). Four tests we have
specified fail on `main` today. This work fixes correctness bugs that already exist.

---

## 2. User story

> **Anya is formalizing a paper with 20 labeled theorems in Overleaf.**
> She marks them all and asks Lea to formalize the document. Today, Lea proves them
> **strictly one at a time** — the second request is rejected with a `409`, and the
> Overleaf companion retries it in a loop until the first finishes. A 20-theorem paper is
> a 20× serial wait, on a machine that is idle for most of it.
>
> **Anya then hits one genuinely hard lemma.**
> Today Lea attacks it the way a single-threaded program would: search Mathlib, try a
> tactic, fail, backtrack, try another — all in **one conversation**, burning turns and
> filling its context with dead ends. A mathematician would not do this. They would try
> two or three approaches, see which bites, and keep that one.

Both problems have the same root cause: **one loop, one context, one file, one lock.**

What Anya should get instead: her 20 theorems formalize concurrently; her hard lemma gets
several proof strategies explored **in parallel** by scoped subagents, with the failures
absorbed by throwaway contexts and only the winning proof promoted into her document.

---

## 3. Where we are today

See `architecture-figure-d2.html` for the current backend. In one line: the **FastAPI
adapter** drives the **vendored prover in-process** (no HTTP boundary); the prover runs a
six-tool loop (`read_file · write_file · edit_file · lean_check · bash · search_mathlib`)
against a shared **Lake/Mathlib workspace**, a warm **LSP daemon**, and **SafeVerify**;
**Git owns proof content**, **SQLite is a rebuildable index**.

The relevant constraint sits in the adapter:

```python
# adapter/app/bridge.py — one activation at a time
active_run_lock = Lock()   # "they share the on-disk workspace and the warm LSP daemon"
```

That comment is the **entire safety argument for the system**, and both halves of it are
load-bearing.

---

## 4. The problem

### 4.1 One loop is a poor fit for proof search

Proof search is naturally parallel and speculative: try several premises and tactics,
discard what fails. Lea's single loop makes every attempt **sequential** and every failure
**permanent context pollution** — dead ends stay in the conversation, consuming the window
and biasing later turns. This is the structural reason a serial loop underuses both the
hardware and the model.

### 4.2 The lock is hiding correctness bugs (the part that matters most)

Auditing what the lock protects turned up hazards that are **already latent**. The two
serious ones:

- **The Lean daemon can return another session's verdict.** `lsp_daemon.py` is a
  *synchronous shim*, not a real LSP client: it holds its lock only around a registry
  lookup, destructively purges a single shared queue on entry, and **drops** messages whose
  URI doesn't match rather than requeuing them. Two threads checking the *same* file →
  thread A can return a verdict computed from **thread B's file content**. That feeds the
  final gate and produces a false **"Proved."**
- **SafeVerify cross-contaminates.** Scratch files are keyed by **file stem only** in one
  shared directory. Two sessions both working on `Div6.lean` verify **A's submission
  against B's target** — a wrong verdict on the strongest claim the system makes.

Plus infrastructure hazards: no SQLite WAL/`busy_timeout` (`database is locked` thrown into
run threads), `git add -A` cross-staging between sessions, a lock **leak** that permanently
409s every later run, and a cold-fallback herd that spawns N Mathlib compiles on one LSP
hiccup. Full inventory (H1–H12) in `v2.3-concurrent-runs-architecture.md` §2.

**Why this matters for review:** these are not risks introduced *by* the proposal. They are
bugs that exist now, masked by a lock that also happens to block the feature we want. The
proposal fixes them first (§6, Phases 0–2) and only then removes the lock.

---

## 5. The key insight — Lean already solved this

The Lean 4 server is **not** a synchronous request/response loop. It is a watchdog process
coordinating **one file-worker process per open file**, and its design explicitly states
that *"file processing and requests+notifications against a file should be concurrent"* —
for cancellability and fault isolation [4].

Every real Lean client (vscode-lean4, lean.nvim, coc-lean) does what JSON-RPC requires: a
reader loop dispatching responses by request `id` and notifications by `uri`. **Lea's
daemon is the anomaly** — a shim written against a mistaken model of the protocol. So
making it multiplex is not a risky new design; it is *correcting* the component in the
direction every other client already went.

Consequence: **one server, one resident Mathlib, N open documents, N parallel file
workers.** Sessions own distinct files; subagents check distinct candidate files. Lean
gives us the parallelism for free once the client stops serializing it.

---

## 6. What we propose

Staged so the tree is never broken and the structural rewrite ships **behind a flag set to
the old value**. Full table in the companion doc §5.

| Stage | What | User-visible? |
|---|---|---|
| **0–2** | Fix the substrate: SQLite WAL + `BEGIN IMMEDIATE`; path-scoped git commits; **rewrite `lsp_daemon.py` as a real LSP client**; per-call SafeVerify scratch; bound expensive fallbacks | **No** — lock still on |
| **3–5** | Run registry; admission control moves into the endpoint; path ownership + resource locks; UI reattach backoff | No — ships at `MAX_CONCURRENT_RUNS=1` |
| **6** | **Flip the cap to 4** | **Yes — concurrent runs** |
| **7–8** | **Subagents**: child activations, declarative agent profiles, budgets, candidate collation, proof-context compaction | **Yes — multi-agent** |

### The subagent model (D76)

Subagents **never write the canonical proof file.** They write and check *temporary
candidate files* under an ignored per-run tree (`.lea/tmp/<run>/<agent>/`), then return
**distilled results** — checked candidates, imports, helper lemmas, failures, diagnostics.
The parent collates and commits one clean artifact.

| Agent | Mode | Permissions | Purpose |
|---|---|---|---|
| `formalizer` | primary | canonical write, commit, SafeVerify, spawn | Coordinator for a theorem session |
| `plan` | primary | read-only | Decompose theorem, choose strategy |
| `premise-search` | subagent | read/search only | Find Mathlib lemmas, imports, patterns |
| `proof-candidate` | subagent | temp writes + `lean_check` | Try proof candidates in isolation |
| `critic` | subagent | read-only | Explain diagnostics, suggest next attempts |
| `refactor-candidate` | subagent | temp writes + `lean_check` | Explore helper lemmas / file splits |

**Why this shape:** it makes same-lemma parallelism tractable **without CRDTs or merge
theory**. The expensive work (search, attempts, Lean checks on distinct URIs) is parallel;
the canonical file has exactly **one writer**. Candidate output is trusted only as
*evidence* — the parent re-checks the promoted file and final SafeVerify is unchanged.

---

## 7. What we borrow, and from where

We audited two production agent harnesses at the source level (commit SHAs in §11).

| We adopt | From | Specifically |
|---|---|---|
| Delegation tool shape (`{description, prompt, subagent_type, resume}`), result envelope, depth cap | **opencode** | `tool/task.ts`; result wrapped as `<task id state><task_result>`; depth capped by walking the parent chain (default 1) [1] |
| **Permission composition** (D79) | **opencode** | `deriveSubagentSessionPermission`: a child gets *its own* capabilities ∪ *the parent's deny rules*. Restrictions **only tighten** through delegation [1] |
| **Resource-declaring tools** (D77/D80) | **OpenHands SDK** | `DeclaredResources` + `ResourceLockManager`: parallel tool calls serialize *only* on shared resources; sorted-order acquisition (deadlock-free); per-prefix timeouts [3] |
| **Path-triggered knowledge** (D81) | **OpenHands SDK** | `PathTrigger`: touching a file injects domain-scoped guidance — for us, `Mathlib/MeasureTheory/` → measure-theory tactics [3] |
| **Declarative agent profiles** (D78) | **both** | Markdown + YAML frontmatter (`model`, `tools`, `permission_mode`, budgets); body = the role's prompt [1][3] |
| **Composed prompt sections + cache tiering** (D82/D84) | **OpenHands SDK** | Static blocks first, volatile tail last, to keep the prompt-cache prefix stable [3] |
| **Tested role prompts** (adapted for Lean) | **both** | `explore.txt` → `premise-search`/`critic`; `bash_runner.md` → `proof-candidate`; `planning.py` → `plan`; `task.txt` → the spawn tool's description [1][3] |

**Why borrow rather than invent:** these are the parts of an agent harness where a subtly
wrong design fails *silently* — permission leaks through delegation, deadlocks under
parallel tools, prompt drift between roles. Both projects have already paid for those
lessons in production.

---

## 8. Design choices — the tradeoffs worth your review

These are the forks where we chose deliberately. Each is a place to push back.

**8.1 Fix `lsp_daemon.py` in place · *rejected:* adopt `leanclient`.**
✅ *For fixing:* the observability fork already uses `leanclient` — and wraps every check in
a coarse lock, **re-introducing exactly the serialization we're removing**. It also does
open → diagnostics → **close** per call, discarding the warm-document model that makes
rechecks 0.2s instead of 88s. And it adds a dependency to a vendored package with a pinned
toolchain.
❌ *Against:* we own a transport rewrite (~150 lines of a 317-line file) instead of using a
library.
**Call:** fix in place. The library's design contradicts our core perf property.

**8.2 Keep the `409` as "at capacity" · *rejected:* a server-side queue.**
✅ *For 409:* the Overleaf companion **already** treats 409 as a queue signal (retry with
jitter, adopt terminal outcome). Redefining it is a semantic **no-op** — zero companion
changes. The Overleaf test suite staying green *without modification* is our proof the
decision was right.
❌ *Against:* clients must retry rather than being told when a slot frees.
**Call:** keep it. A queue pins a connection + task per waiter, makes run ownership
ambiguous on disconnect, and eats the browser's 6-connections-per-origin budget.
*Corollary:* removing the 409 without admission control would be **worse than today** — a
20-theorem document would fire 20 simultaneous provers.

**8.3 Path ownership inside a shared project repo · *rejected:* a git worktree per run.**
✅ *For ownership:* the primary Overleaf case puts all N theorems of a document in **one
project**; serializing per project would deliver **zero** concurrency for it.
❌ *Against:* we must enforce path leases at the tool boundary.
**Call:** ownership. A worktree would have to live under `workspace/proofs/` (Lean's
`srcDir`) or imports break — and that **changes every module name**.

**8.4 Subagents write temp candidates · *rejected:* subagents write the canonical file.**
✅ *For temp:* one writer per file makes same-lemma parallelism tractable with no merge
model. Failures cost nothing.
❌ *Against:* an extra promote/collate step, and candidate work is thrown away.
**Call:** temp candidates. Direct writes would need CRDTs or character-level merge.

**8.5 Subagent prompts *compose* the Lean core · *rejected:* opencode's replace-the-prompt.**
This is **safety-critical**. In opencode, an agent's own prompt *replaces* the base entirely.
If we did that, a `proof-candidate` subagent could lose the rule **"never modify the theorem
statement"** and quietly "prove" a weakened statement — the exact class of cheat SafeVerify
exists to catch [5].
**Call:** compose — `shared Lean core (hard rules + tactic cascade) + per-role head`, core
non-overridable. Roles may only *narrow* capability, never *drop* the invariant.

**8.6 Keep `code_steps` · *rejected:* a generic timeline-events table.**
✅ *For:* Lea's durable artifact is Lean proof content; status derivation, canvas hydration
and history stay simple and explicit.
❌ *Against:* less extensible — a new timeline concept needs a small purpose-built table.
**Call:** keep. Documented in `STORAGE-code-steps-vs-timeline.md`.

---

## 9. Benefits

**Correctness (immediate, independent of the feature).** Fixes latent false-`Proved`
paths (LSP cross-talk, SafeVerify cross-contamination) that exist on `main` today. For a
system whose entire value proposition is *"this proof is verified,"* a wrong-verdict path
is the most severe possible bug.

**Throughput (Stage 6).** Anya's 20-theorem document formalizes concurrently instead of
serially, on one resident Mathlib. Target: 6 concurrent runs in **< 2×** the wall time of a
single run.

**Proof capability (Stages 7–8) — a hypothesis, not a promise.** Parallel strategy
exploration plus domain-scoped tactics *should* raise pass rates on hard problems. We do
**not** claim this yet — it is the thing to measure (§10).

**Context economy.** Failed proof attempts are absorbed by throwaway subagent contexts
instead of polluting the main conversation. The coordinator sees distilled results, not
dead ends — directly attacking the "endless searching" failure mode the current prompt
tries to police with a hard 20-search budget.

**Specialization.** `_TACTIC_CASCADE` is today **one flat table for all of mathematics**, so
`Measurable f` and `a ≤ b` over ℝ compete for the same prioritization. Path-triggered
cascades (D81) give each domain its own ladder.

---

## 10. How we will know it works

**Correctness first — the test we write before any code.** A deterministic unit test
(`test_dispatch_unit.py`, no Lean, milliseconds): a fake LSP server emits frames in an
**adversarial order** while two threads check different files. Assert **each thread gets its
own file's diagnostics**. *On today's code this test cannot even be written* — there is no
transport seam — and the code it would test is wrong two separate ways.

**End-to-end.** Six concurrent runs, three provable and three false. Assert **exactly** the
three provable ones end `proved`. Any LSP/SafeVerify/git cross-talk surfaces as a mislabeled
session.

**Regression as proof of design.** The Overleaf suite must stay green **without
modification** — that is the evidence 8.2 was right.

**Four specified tests fail on `main` today** (`test_db_seq`, `test_gitstore_concurrent`,
`test_bridge_divergence`, `test_concurrent_checks`) — evidence the hazards are real, not
theoretical.

**Capability (the honest part).** Pass-rate impact of subagents is measured against our
existing SafeVerify-audited harnesses (miniF2F, FormalQualBench) per `EVALS.md`, comparing
single-loop vs coordinator+subagents at matched token budget. **Matched budget matters**: N
parallel attempts spend N× tokens, so any pass-rate gain must be shown to beat simply giving
the single loop the same budget. If it doesn't, Stages 7–8 are not justified on capability
grounds and stand only on throughput.

---

## 11. Open questions for review

1. **Is matched-token-budget the right eval control** for single-loop vs multi-agent, or
   should we compare at matched wall-clock (the thing the user actually feels)?
2. **Stages 0–6 are pure infrastructure** with no capability story. Is fixing the latent
   wrong-verdict bugs sufficient justification on its own, or should they be bundled with
   Stage 7 for a single reviewable result?
3. **Subagent depth is capped at 1.** Is there a proof-search argument for depth 2
   (e.g. a `proof-candidate` spawning its own `premise-search`)?
4. **Literature.** This proposal cites implementations, not papers. If it should sit
   against related work on multi-agent LLM proof search / premise selection, that review
   is not yet done and should be added before circulating further.

---

## 12. References

**Audited sources** (read at these commits; both are ephemeral local checkouts — copy any
prompt we adopt into `prover/lea/agents/` before they are lost):

[1] **opencode** — `https://github.com/anomalyco/opencode` @ `4394b32` (2026-07-15).
    `packages/opencode/src/tool/task.ts` (delegation, depth cap, result envelope);
    `src/agent/subagent-permissions.ts` (permission composition);
    `src/permission/index.ts` (ruleset evaluation, tool visibility);
    `src/config/agent.ts` + `.opencode/agent/*.md` (markdown agent definitions);
    `src/agent/prompt/explore.txt`, `src/tool/task.txt` (adopted prompts).

[2] **OpenHands** — `https://github.com/OpenHands/OpenHands` @ `9c5d4dc` (2026-07-15).

[3] **OpenHands Agent SDK** — `https://github.com/OpenHands/software-agent-sdk` @
    `51c102b` (2026-07-15).
    `openhands-sdk/openhands/sdk/tool/tool.py` (`DeclaredResources`);
    `sdk/conversation/resource_lock_manager.py`, `sdk/agent/parallel_executor.py`;
    `sdk/skills/trigger.py`, `sdk/context/agent_context.py` (path-triggered injection);
    `sdk/subagent/schema.py`, `subagent/load.py` (profile frontmatter + discovery);
    `sdk/context/prompts/sections/*.py`, `presets.py` (composed prompts, cache tiering);
    `openhands-tools/openhands/tools/preset/subagents/*.md` (adopted prompts).

[4] **Lean 4 language server design** —
    `https://github.com/leanprover/lean4/blob/master/src/Lean/Server/README.md`
    (watchdog + per-file workers; concurrency is intended).

**Internal:**

[5] `apps/lea-standalone/prover/DESIGN.md` — prover guardrails (immutable theorem
    statements; no `sorry`/`axiom`/`native_decide`); `EVALS.md` — SafeVerify-audited
    methodology; `tests/cheats/` — regression suite proving the grader rejects known cheats.

[6] `design/v2.3-concurrent-runs-architecture.md` — full technical design, hazards H1–H12,
    decisions D69–D84, staging and verification plan.

[7] `docs/STORAGE-code-steps-vs-timeline.md` — schema decision (8.6).
