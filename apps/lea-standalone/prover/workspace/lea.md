# Project-Specific Workflow — coordinator + sub-agents

This project runs Lea as a **coordinator that delegates**, not a single agent that
does everything itself. Read the section that matches the tools you were given.

## If you have the `spawn_subagent` tool — you are the COORDINATOR

Your job is to **orchestrate a proof against a blueprint, not to grind it out in
your own context.** Every `lean_check`, `read_file`, and dead-end you run yourself
stays in your transcript forever and is re-sent every turn — that is the main way a
run becomes slow and expensive. A sub-agent explores in a throwaway context and
hands you back only a distilled result, so delegation is how you keep your own
context small.

**Two roles are available** (`subagent_type`):

- **`premise-search`** — a read-only Mathlib scout. Give it a goal; it returns the
  lemma names + signatures, the `import` lines, and the tactic pattern most likely
  to discharge it. It cannot write or check. Use it *before* you commit to an
  approach, when you don't yet know the Mathlib names.
- **`proof-candidate`** — grinds one full proof attempt (up to 150 turns) in an
  isolated scratch file and reports the candidate's path + its `lean_check` verdict.
  Use it to *offload the write→check→edit loop* for a self-contained lemma.

### Maintain `blueprint.md` — it is your plan of record

Write a file named **`blueprint.md`** in your working directory. It is *not* a proof
file (never checked or promoted), and it is **not** how you track verdicts
moment-to-moment: you already see every `lean_check` result and every sub-agent
report in your own context, so **do not re-read the file to recover what you just
saw** — that only re-spends tokens on state you already hold. The blueprint is a
*durable write*. Its job is to hold the decomposition + status so the plan survives
when your context is later compacted or the session is resumed, and so a human (and,
in future, a seeded child) can read the plan. Write it once after you decompose, then
update a row only at a real milestone — a sub-lemma flipping to `PROVED`/`FAILED`.
Read it back only when your context has actually been trimmed (post-compaction or
resume) or to re-anchor the final assembly — never as a per-turn habit.

**The loop:**

1. **Decompose first, then write the blueprint.** Break the theorem into independent
   sub-lemmas — each one a statement that can be proved on its own — and write
   `blueprint.md` (template below) with every sub-lemma marked `TODO`.
2. **Work one sub-lemma at a time.** Pick a `TODO` row. If you don't yet know the
   Mathlib names, spawn `premise-search` on it first. Mark the row `DELEGATED`, then
   spawn a `proof-candidate` whose prompt states that sub-lemma's **exact signature
   as the GOAL** — never a file path (any path you pass is ignored; the child writes
   in its own scratch dir).
3. **Collate — one check, on the assembled file.** The child's result already hands
   you the **candidate path and its `lean_check` verdict** — take them as given.
   `read_file` that path (never `bash ls` to hunt for the file), and **do not re-check
   the candidate on its own**: the child already drove it clean, so re-running
   `lean_check` on the isolated file is wasted work. Assemble it into your canonical
   `.lean` file and run `lean_check` on **that** file — a *single* check that validates
   the **merge** (different imports, namespace collisions, shadowing the isolated
   candidate never saw) and, re-elaborated in the real context, catches any
   `sorry`/namespace cheat. Update the row → `PROVED` (with the child's result id) or
   `FAILED` (with the blocking error). The child's verdict is *evidence* for the
   isolated lemma; your check on the assembled file is the *verdict of record*.
4. **Repeat until every row is `PROVED`,** then assemble the sub-lemmas into the
   final theorem and `lean_check` the whole file. Keep the blueprint's Assembly
   section in step with what actually compiles.
5. **Do the small stuff yourself.** A one-line `exact`/`simp` closes faster inline
   than a spawn round-trip. Delegate what is *worth* an isolated context — real
   proof-grinding and open-ended Mathlib hunts — not trivia.

**`blueprint.md` template:**

```markdown
# Blueprint — <the goal in one line>

## Goal
`theorem <name> ... : <statement> := by`   -- the exact top-level target, verbatim

## Strategy
One paragraph: how the sub-lemmas below chain together to close the goal.

## Sub-lemmas
| id | statement (Lean signature)      | status | result             | notes            |
|----|---------------------------------|--------|--------------------|------------------|
| L1 | `lemma l1 (…) : …`              | TODO   | —                  | —                |
| L2 | `lemma l2 (…) : …`              | TODO   | —                  | depends on L1    |

## Assembly
How L1…Ln combine into the final proof — filled in as sub-lemmas land.
```

Statuses flow `TODO → DELEGATED → PROVED` (or `FAILED`, with the error that blocked
it so the next attempt starts from a real obstacle, not a vague one).

(Sub-agents currently execute one at a time, so treat delegation as *context
isolation*, not speed — the win is keeping dead ends out of your transcript. Each
child cannot itself spawn, so decompose to the level you want proved directly.)

## If you do NOT have `spawn_subagent` — you are a delegated WORKER (or a direct run)

Do your one task directly and well. Write your candidate as a **relative filename**
in your working directory (e.g. `candidate.lean`) — if a task names an absolute path,
use only its filename; that path belongs to the coordinator, not you. Your final
message is your result: state plainly whether the file compiles cleanly (no errors,
no `sorry`) and what it proves.
