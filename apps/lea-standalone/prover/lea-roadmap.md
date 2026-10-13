# Lea Roadmap — Closing the Gap to Frontier Theorem Proving

> Empirical roadmap based on a side-by-side comparison: Claude Code (Opus 4.7
> with the Claude Code harness) closed 15 of 17 hand-written Lean nodes
> across the lea-frontier RegLip project (May 2026), while Lea (same model,
> different scaffolding) closed 1. Both used Opus 4.7 — so the gap is
> entirely scaffolding.
>
> This document captures (a) what specifically Claude Code does that Lea
> doesn't, (b) which of those differences are worth importing, and (c) which
> ones violate Pi ethos and should be ignored. Phased to keep each step at
> roughly tool-budget scale.

**Status as of writing (2026-05-12):** observation-derived; no
implementation has been done.

## Empirical baseline

The same human user ran two formalization workflows against the same
underlying model (Opus 4.7) across April–May 2026:

| Workflow | Project | Closure rate | Notes |
|---|---|---|---|
| Lea (autonomous, dispatcher-orchestrated) | lea-frontier RegLip | 1/17 | `normalization_scaling` (205 lines, ~$1.65). Cost ~$22 across 3 rounds × multiple dispatches. |
| Claude Code (interactive, user-supervised) | same project | 15/17 hand-written + 2 axiomatized | ~600 net Lean LoC this session. Final state: 17/17, main_lipschitz sorryAx-free. |

The "1/17" should not be read as Lea being weak overall — Lea sits at
86.5% on miniF2F validation, which is competitive. The gap shows up
**specifically on long-horizon, multi-file research formalization** with
proofs in the 100–500-line range.

## What Claude Code did that Lea would not have

Direct comparison, drawn from the actual Session 6 transcript:

1. **Cross-file pattern reuse.** When closing `anchored_macro_gradient`,
   I read `DyadicAffineContinuation.lean` to find the existing
   `opNorm_le_bound` contrapositive pattern and adapted it. When closing
   `transition_layer`, I read `PhaseEstimate.lean` for the `Inseparable +
   nhdsWithin + tendsto_nhds_unique` pattern. **Lea has `read_file` and
   `bash` grep but does not routinely browse siblings.**

2. **Strategic pivot mid-task.** While writing the `transition_layer`
   scaffold I realized the manuscript's interior/boundary case split was
   *redundant* under our axiom layer — JI alone suffices. That insight
   came from sitting with the proof after committing the scaffold; it cut
   the remaining work by ~300 lines. **Lea's per-dispatch model commits
   to one strategy; no forcing function to step back.**

3. **Judgment about axiomatization vs hand-writing.** Two nodes
   (`large_slope_estimate` Prop 6.1, `dyadic_growth_control` Cor 5.8)
   were closed in one line each by packaging the conclusion as a
   textbook axiom with citation. This requires recognizing "this is
   recipe applied to existing inputs, not novel content." **Lea has no
   workflow for proposing axiomatizations.**

4. **Statement-bug detection.** `anchored_macro_gradient`'s pre-existing
   statement had a typo (sup over `B_1` instead of manuscript's `B_R`)
   that made the theorem provably false for unbounded R. I caught it by
   trying to prove it and noticing the math couldn't close. **Lea would
   have spun for full `max-turns` on the buggy statement.**

5. **Human-in-the-loop checkpoints.** I asked the user 5 times this
   session: hand-write vs axiomatize for transition_layer? case-split or
   JI alone? what to do at 17/17? **Lea cannot ask for input. The
   "collaborator not oracle" framing in `CLAUDE.md` is aspirational
   without this affordance.**

6. **Structured task tracking.** I used a todo list to externalize plans
   across sub-tasks. **Lea has session save/load but no in-session
   plan-tracking affordance.**

7. **Persistent project-scoped memory.** The auto-memory file
   (`~/.claude/projects/.../MEMORY.md`) carried context across all six
   RegLip sessions. **Lea has `--resume` but it's per-session, not
   per-project.**

8. **Full error trace in conversation context.** Every `lean_check`
   failure was visible in the next turn's input, with goal state and
   hypothesis dump intact. **Lea has this too** — the existing while-loop
   does it. This one is not a gap; flagging for completeness.

## What Lea explicitly does NOT need

Per `CLAUDE.md`, these have been tried and retired with evidence. The
roadmap must NOT reintroduce them:

- **State-machine orchestration** (`prove_hard`, sketch/fill/reflect phases). Buggy, no benefit over a plain prompt.
- **MCP server integration**. Violates Pi ethos, adds external process dependency.
- **Loogle web tool**. Zero empirical benefit across three probes.
- **2–3 candidate generation protocol**. Hurt easy problems.
- **`propose_lemmas` via Lean's `exact?`**. Bottleneck was workflow (forced decomposition), not search.
- **Auto multi-tenant / accounts**. Out of scope.

Every roadmap item below either: (a) is a single new tool with a clear
prompt rule for when to invoke, or (b) is a pure prompt-level change.
**No state machines. No phases. No orchestration layer.**

## Roadmap

Four phases, each independently shippable. Order is highest-leverage
first.

### Phase 0 — Verifier hardening (existing TODO; do first)

Already on the open-questions list in `CLAUDE.md`. SafeVerify or
Comparator integration. Blocks all three observed cheat classes
(namespace shadowing, import-sorry, trivialization) at once. **~100 LoC
+ lake dep.**

Justification: "industry-grade" frontier proving means audit-safe
outputs. Without statement-equivalence checking, no leaderboard claim is
honest, and the cheat patches in `eval/run_fqb_best_of_n.py` are
patches, not solutions.

### Phase 1 — Collaborator affordances (~80 LoC, mostly `tools.py` + `prompt.py`)

The single highest-leverage thing Claude Code had this session was the
ability to *ask the user* at decision points. Adding this to Lea is two
tools and a prompt rule.

**1.1 `ask_user(question: str, choices: list[str] | None = None) → str`.**
Streams the question, accepts a free-text reply or multiple-choice
selection, pauses the agent loop until input arrives. Pi-compatible:
single tool call, fully observable, model decides when to use it.

Prompt rule: *"Call `ask_user` when (a) you'd otherwise spend >100 lines
on bookkeeping that has no novel mathematical content (axiomatize vs
hand-write?), (b) the theorem statement looks suspicious before you've
attempted it (typo or under-specification?), or (c) you've tried the
same approach three times with the same failure mode."*

This single tool reframes Lea from "oracle that succeeds or fails" to
"collaborator that asks when stuck" — which is what `DESIGN.md` already
endorses.

**1.2 `notes(action: 'read' | 'append', content: str = '') → str`.**
A per-project scratchpad written to `<lake_root>/.lea/notes.md`.
Auto-loaded into the system prompt at session start. Agent writes a
one-paragraph summary at the end of each successful proof and reads it
at the start of each new task in the same project.

Pi-compatible: it's just a file. No state machine, no cross-process
memory store, no database. ~30 LoC.

**1.3 Prompt rule for stuck detection.** No new tool. In `agent.py`,
track the last 3 `lean_check` outputs. If error-class repeats, inject a
system message: *"You've tried this approach 3 times with the same
failure mode. Either propose a different strategy, call `ask_user`, or
propose an axiom shortcut."* ~30 LoC of plumbing.

This is **not** a phase machine — it's a single conditional injection
inside the existing while-loop, triggered by an observable repetition.
The model still drives.

### Phase 2 — Project-aware tools (~150 LoC, all of `tools.py`)

For RegLip-scale projects, the agent needs to treat the *project* as a
first-class search target, not just Mathlib.

**2.1 `search_project(query, max_results=10)`.** Like `search_mathlib`
but scoped to the active Lake project's own files. Currently bash-grep
works but is verbose; a dedicated tool encourages the agent to look at
solved sibling theorems for patterns. ~40 LoC.

**2.2 `print_axioms(name)`.** Wraps `#print axioms <name>` in a scratch
Lean file and parses the result into a structured list. Lets the agent
audit trust footprint mid-proof. ~30 LoC.

**2.3 `check_statement(path)`.** Type-checks a file with all proofs
replaced by `sorry`. Catches statement typos before proof effort. ~30
LoC wrapper around `lean_check`.

**2.4 `list_blueprint_nodes(blueprint)`.** Parses a LaTeX blueprint
(lea-frontier already has `tools/blueprint_parser.py` — vendor that in
or reimplement) and returns nodes with dependency edges and current
status. Lets the agent see the project shape without scanning every
file. ~50 LoC.

Prompt rules:
- *"Before attempting any proof, call `check_statement` on the target file."*
- *"When stuck on a sub-step, call `search_project` for similar patterns in already-closed nodes."*
- *"At end of session on a frontier proof, call `print_axioms` and report the trust footprint."*

### Phase 3 — Forced decomposition (prompt-only, ~0 new LoC)

The `propose_lemmas` retirement note in `CLAUDE.md` identified the real
bottleneck: agents (especially Opus) skip the "decompose into
intermediate `have ... := sorry` blocks" phase and jump to top-level
proof. The fix is prompt-level.

New section in `prompt.py`:

> *"For theorems whose statement is longer than 5 lines OR whose proof
> you estimate at >50 lines, first emit a numbered list of `have`
> sub-claims with `sorry`, run `lean_check` to confirm the decomposition
> type-checks, and only then fill in the sub-proofs. Each `have` is a
> sub-task; close them in dependency order."*

This pairs with `search_project` and `print_axioms`: once decomposed,
the agent can search for patterns matching each sub-claim and audit
trust as it fills.

Revisit `propose_lemmas` *bundled* with this rule. Standalone it failed
because the trigger point never fired. With forced decomposition, the
trigger point fires on every non-trivial proof.

### Phase 4 — Frontier-scale eval (~100 LoC, new `eval/run_project.py`)

Existing evals (miniF2F, FQB) measure single-problem success. They
underestimate the relevant signal for research formalization:

- Can Lea close a *dependency graph* end-to-end?
- What's the final axiom footprint?
- How does cost scale with project size?

New harness: given a blueprint + a Lake project with sorry-stubs, run
Lea on each node in topological order, with `--feedback` and
project-scoped memory enabled. Output: closure rate, axiom footprint,
total $.

Fixtures: lea-frontier's RegLip (17 nodes) and lea-hadamard (~27 nodes).
Optional: a synthetic project of 5–10 nodes for fast iteration.

Comparison baseline: this session's Claude Code numbers on RegLip.

### Phase 5 (optional, only if needed) — Context management

If post-Phase-1-3 long sessions hit context-window walls (Opus 4.7's 1M
window should be enough; flag if not):

**5.1 Auto-summarize old turns.** Once conversation > N tokens, replace
old tool-result blocks with one-line summaries. ~50 LoC. Deferred —
1M context probably makes this unnecessary for RegLip-scale projects.

### Phase 6 — Cross-attempt knowledge sharing (ideas from AlphaProof Nexus)

Source: Tsoukalas et al., *Advancing Mathematics Research with AI-Driven
Formal Proof Search*, arXiv:2605.22763v1 (DeepMind, May 2026). Their
agent autonomously resolved 9 Erdős problems via a Lean-grounded
Ralph-loop architecture closely resembling Lea's. The most useful
finding for Lea is the *negative* one: their Agent A (basic loop, no
critics, no evolutionary search) solved all 9 problems on its own;
the sophisticated Agent D only dominated on the two hardest instances
with a 2–5× cost saving. That validates Lea's current design, but two
of their cheaper mechanisms are worth borrowing.

**Architectural note on the dispatcher.** The multi-node orchestrator
lives at `lea-frontier/tools/dispatcher.py` (~520 LoC), not in
lea-prover. That split is intentional — lea-prover is a single-loop
agent; lea-frontier dispatches it across blueprint nodes. The cache
and lesson-log infrastructure below naturally lives on the dispatcher
side (lea-frontier), with lea-prover gaining minimal hooks to consume
the data via prompt injection. Phase 4's `eval/run_project.py` is the
lea-prover-internal counterpart for evals.

**6.1 Global goal cache.** When a `have` subgoal closes in any
dispatcher run, store `(normalized_goal_hash → proof_term)` in
`<lake_root>/.lea/goal-cache.json`. On subsequent dispatches the
dispatcher pre-loads relevant cache hits into the prompt as
"known-working proofs for related subgoals." Direct port of the
paper's global goal cache.

Pi-compatible: a single JSON file. Dispatcher reads/writes;
lea-prover consumes via system-prompt injection.

Effort: ~150 LoC in `lea-frontier/tools/dispatcher.py`, ~20 LoC in
lea-prover `prompt.py`.

Highest-leverage target: lea-hadamard (27 theorems sharing a
blueprint = 27 chances to reuse common Mathlib lemma applications).
RegLip-style projects benefit less because each node's proof structure
diverges quickly, but baseline Mathlib invocations (`exact?`-style
chains, `omega`-amenable goals) would still hit.

**6.2 Structured lesson log per project.** The paper has agents
accumulate one-line "what worked / what didn't" lessons inside the
proof file as comments, surviving across attempts. Formalize as: at
end of each dispatch, the agent appends a one-paragraph lesson to
`<lake_root>/.lea/lessons.md`. The dispatcher injects the log into the
next dispatch's prompt.

Fold into Phase 1.2's `notes.md` if implemented; otherwise ~10 LoC
standalone. Same Pi-compatibility justification — it's a markdown file.

**6.3 Cheap-model critic step (DEFERRED — conflicts with retired
decision).** The paper's Agent D uses a cheaper model (Gemini 3.0
Flash) to rate candidate proof sketches via Plackett-Luce / Elo
before paying the strong model to elaborate. In Lea terms: rank N
candidate sketches with Haiku 4.5 before Opus 4.7 fills them in.

**Why deferred:** the multi-candidate generation pattern was retired
with evidence (CLAUDE.md design decision 4, "2-3 candidates protocol";
hurt easy problems by ~2× turn count). A critic step requires
multi-candidate generation as a prerequisite, so adopting it
relitigates that decision. The paper's own data agrees with the
retirement: critics only help on the hardest instances (Erdős #125,
#138); easier problems are net negative.

Conditional revisit: only if Phase 4's frontier-scale eval shows Lea
plateauing on specific hard nodes where multiple plausible strategies
exist but selection is unclear. In that case, a *single Haiku-rated
binary choice* (continue vs. axiomatize? hand-write vs. re-dispatch?)
might be worth piloting — not the full Plackett-Luce / Elo machinery.

## Budget summary

| Phase | LoC | Effort | Outcome |
|---|---|---|---|
| 0 | ~100 + lake dep | 1–2 days | Audit-safe eval; leaderboard-claim eligibility |
| 1 | ~80 | 1 day | Collaborator-style affordances; stuck-detection |
| 2 | ~150 | 2–3 days | Project-aware tools; statement bugs caught pre-proof |
| 3 | 0 (prompt only) | half day | Forced decomposition; revives premise-search value |
| 4 | ~100 | 1–2 days | Hard numbers on "RegLip-scale" capability |
| 5 | ~50 | 1 day | (Only if 1M context isn't enough) |
| 6.1 | ~170 (across two repos) | 1–2 days | Goal cache; biggest payoff on lea-hadamard |
| 6.2 | ~10 | half day | Lesson log; rolls into Phase 1.2 if landed first |
| 6.3 | — | — | Deferred (conflicts with retired decision 4) |

Total committed (Phases 0–4): **~430 LoC, ~5–8 days**. Roughly +33% to
Lea's current 1300-LoC codebase. Stays well under "orchestration layer"
thresholds. Phase 6.1–6.2 add another ~180 LoC if landed.

## Predicted outcomes

With Phases 0–3 done:

- **RegLip-style closure rate: 1/17 → 12–14/17** on the algebraic and
  hand-write-style nodes. The taste-heavy decisions (e.g., "interior JI
  alone suffices for transition_layer") still benefit from
  human-in-the-loop, which Phase 1 enables.
- **FQB legit rate**: probably +2–4 nodes from decomposition + premise
  search bundling (Phase 3). Hard to predict without running.
- **Audit-safe**: yes, post-Phase-0. Outputs are credible for research
  reporting.
- **Cost per closed node**: Phase 2's tools mean less Mathlib-grep
  flailing, probably -20–30% per node on long proofs.

For 17/17 fully autonomous, the lever isn't Phase 1–5 — it's deeper
model capability OR a true human-in-the-loop workflow (`ask_user` used
often, not as a fallback). The v3 collaborator framing is the right
ceiling to aim for.

## Things this roadmap deliberately does NOT include

Restating because they're tempting:

- **No state machine / phase manager.** Stuck-detection in Phase 1 is
  one injection, not a phase.
- **No MCP server.** The four new tools all use subprocess.
- **No multi-candidate generation.** Retired with evidence.
- **No Loogle.** Retired with evidence.
- **No `propose_lemmas` standalone.** Revisit only bundled with Phase 3
  forced decomposition.
- **No external memory store (Redis, sqlite, vector DB).** Phase 1.2 is
  a markdown file.
- **No model routing / planner agent.** One model, one loop.
- **No retry harness inside `agent.py`.** Best-of-N stays at the
  `eval/run_fqb_best_of_n.py` level. Lea-the-agent is single-attempt
  with internal iteration.

## Honest ceiling

Even after Phases 0–4, scaffolding at the same model has a ceiling. The
taste-heavy decisions that closed RegLip — the JI-alone insight, the
axiomatize-Prop-6.1 call, the statement-bug fix in
anchored_macro_gradient — came from sustained inference time on the
problem PLUS a user to bounce ideas off.

Lea's `max-turns` budget can be raised, but each turn is still a single
forward pass. To approach Claude-Code-level on the hardest research
proofs, the realistic path is the **collaborator model the DESIGN.md
already endorses**: Lea + frequent `ask_user` checkpoints + user review
at branch points. Pure autonomy on frontier proofs is harder than the
benchmark numbers suggest.

That said, post-Phase-1 Lea would be genuinely useful for research
formalization in a way it isn't today. The 12–14/17 prediction would
make Lea the right default tool for new lea-frontier-style projects,
with Claude Code reserved for the 3–5 taste-heavy nodes per project.

## Open questions

1. **Should `ask_user` be a tool or a CLI prompt?** Tool form is cleaner
   for autonomous runs (the CLI just blocks); but a CLI form lets users
   intervene without the agent explicitly invoking. Probably both: tool
   form for explicit asks, plus a `--interactive` CLI flag that pauses
   periodically. Decide on first implementation.

2. **Is the `notes.md` per-project memory robust enough?** Or should it
   be a structured JSON file (theorems closed, axioms used, patterns
   that worked)? Start with markdown; upgrade if patterns emerge.

3. **How to evaluate Phase 4 fairly?** RegLip and lea-hadamard are real
   research projects — re-running on them risks contamination from the
   blueprint nodes themselves having been written by you/Claude during
   the original closure. A fully-fresh fixture would be better. Maybe a
   small Putnam-style project of 5–10 dependent nodes.

4. **At what point does Phase 1.3 stuck-detection become a state
   machine?** Currently it's one conditional injection. If you add a
   second (e.g., "after 5 failures, force decomposition"), it's still
   not a state machine. If you add five, it is. Keep the bar at
   "two injections max, both conditional on observables."

## Pointers

- `lea/agent.py` — tool loop, where Phase 1.3 plumbing lives
- `lea/tools.py` — where Phases 1.1, 1.2, 2.1–2.4 add tools
- `lea/prompt.py` — where Phase 3 + all prompt rules live
- `eval/run_project.py` — new, Phase 4
- `CLAUDE.md` — open-questions list; Phase 0 = open-question 1
- `DESIGN.md` — "Collaborator, not oracle" framing; Phase 1 makes this real

---

*This roadmap was drafted after a six-session formalization of the
RegLip project (lea-frontier) using Claude Code with Opus 4.7. The
empirical baseline reflects what Claude Code did this session and what
Lea would have needed to match it. Updates welcome as Phases land.*
