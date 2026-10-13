# AUDIT — QuantumAllocation formalization project

Snapshot as of 2026-08-03 (monorepo commit `2fb641c`; project repo commit `992f923`).

Scope: the Lea project at
`apps/lea-standalone/prover/workspace/proofs/Lea/QuantumAllocation/` — the LaTeX
scaffold under `.lea/files/overleaf/` and the Lean produced from it — with a
focus on the remaining targets in `qubo-transformation.tex`.

## Verdict at a glance

| Area | Status |
|---|---|
| Lean modules created | 8 of 25 targets (all of `allocation-definitions.tex`) |
| Lean build | ✅ `lake build` of all 8 modules succeeds (856 jobs, Mathlib v4.29.0) |
| `sorry` / `axiom` / `native_decide` | ✅ none |
| LaTeX ↔ Lean fidelity | ✅ faithful; two minor notes below |
| `qubo-transformation.tex` targets | ⏳ 0 of 17 formalized (12 defines + 5 theorems/lemmas) |
| Scaffold math (encoding, offset, penalty-weight argument) | ✅ checked by hand, sound; one `uses` gap flagged |

Everything formalized so far is a **definition** — no theorem has been proved
yet. All of the actual proof work (5 `lea: formalize` targets, culminating in
the QUBO-correctness theorem) lies ahead in `qubo-transformation.tex`.

## 1. Project state

The project was initialized 2026-07-31 and has four commits in its internal git
(`project init` ×2, `overleaf: mirror LaTeX sources`, and a namespace rename
`Lea.P6a6cec28c6e3d7666bd49566 → Lea.QuantumAllocation`). The LaTeX mirror in
`.lea/files/overleaf/` is byte-identical to the working copy at
`docs/quantum-allocation-overleaf/` (itself untracked in the monorepo).

`.lea/blueprint.md`, `.lea/instructions.md`, and `.lea/memory.md` are still the
empty seed templates — no blueprint nodes were recorded for the 8 completed
targets. Three Lean files (`FeasibleAssignment.lean`, `IsOptimalAssignment.lean`,
`QUBO.lean`) are untracked in the project git; the other five were swept into
the rename commit. Since v2.3 SQL owns proof content and this git is transport
only, so this is cosmetic, not data loss.

## 2. LaTeX scaffold

Three files, all included from `main.tex` (which builds a clean article:
theorem environments defined, section counters set so the include files land on
paper sections 3 and 5, all `\ref`s resolve to labels that exist):

- `allocation-definitions.tex` — paper Section 3: 8 `lea: define` targets
  (`AllocationInstance` … `QUBO`), with a mapping table from paper definition
  numbers to target names.
- `qubo-transformation.tex` — paper Section 5: 12 `lea: define` + 5
  `lea: formalize` targets (details in §4 below). Section 4 of the paper is
  prose-only and deliberately introduces no target.
- The scaffold's stated modeling choices (exact-k safety, Nat-valued data,
  feasibility separate from optimality, corrected chunk encoding, explicit
  objective offset) are consistently carried through both files.

The `uses` dependency graph is topologically ordered top-to-bottom, and every
`uses` entry names either an already-formalized Section 3 target or an earlier
Section 5 target — with **one gap**, flagged in §5.

## 3. Audit of the 8 completed Lean targets

All eight compile with no warnings, no `sorry`, and each matches its LaTeX
definition and `lea: context` directive:

| Target | File | Fidelity |
|---|---|---|
| `AllocationInstance` | `AllocationInstance.lean` | ✅ structure with finite `Node`/`Partition`, `DecidableEq`, Nat-valued `capacity`/`size`/`requests`/`communicationCost`, `k`; no feasibility fields |
| `DataAssignment` | `DataAssignment.lean` | ✅ `abbrev … := I.Partition → Finset I.Node` |
| `ExactKSafety` | `ExactKSafety.lean` | ✅ `∀ p, (D p).card = I.k` — exact, not at-least |
| `RespectsStorage` | `RespectsStorage.lean` | ✅ per-node filtered-univ sum of sizes ≤ capacity |
| `ProcessingCost` | `ProcessingCost.lean` | ✅ Nat double sum over `n ∉ D p` of `requests p n * communicationCost p` |
| `FeasibleAssignment` | `FeasibleAssignment.lean` | ✅ conjunction, reuses the two predicates |
| `IsOptimalAssignment` | `IsOptimalAssignment.lean` | ✅ feasible ∧ minimal among feasible; existence not asserted |
| `QUBO` | `QUBO.lean` | ✅ `weight : V → V → ℝ`; `energy` sums `w i j · x i · x j` over `i ≤ j`; `IsOptimal`; `bitValue` helper |

Minor observations (not errors):

- **N1 — instance fields are not registered as instances.** The bracketed
  `Fintype`/`DecidableEq` fields of `AllocationInstance` are re-activated with
  `letI` in every consumer (`RespectsStorage`, `ProcessingCost`). This works,
  but each of the 17 upcoming targets will repeat the dance, and
  `AllocationQUBOVariable` must additionally *derive* `Fintype`, `DecidableEq`,
  and `LinearOrder` instances to instantiate `QUBO`. A one-time
  `attribute [instance] AllocationInstance.Node_fintype …` (or scoped
  instances) in `AllocationInstance.lean` would simplify everything downstream.
- **N2 — sub-diagonal QUBO weights are dead.** `QUBO.energy` reads only
  `i ≤ j` entries, so two QUBOs differing below the diagonal have identical
  energies. Harmless for the existence theorem `allocationObjective_is_qubo`,
  but any later uniqueness-flavored statement about the weight matrix would be
  false as stated.

## 4. Remaining targets in `qubo-transformation.tex`

Seventeen targets, none started. In file order (which is a valid execution
order):

### Capacity encoding
1. **`CapacityEncoding`** (define) — structure over `C : Nat`: finite linearly
   ordered chunk type, `weight : Chunk → Nat` summing to `C`, an
   `encode t (h : t ≤ C)` producing bits whose weighted value is `t`, plus a
   `weightedValue` helper.
2. **`binaryCapacityEncoding`** (define) — the *corrected* construction: no
   chunks for `C = 0`; for `C > 0`, weights `2^j` for `j < Nat.log2 C` plus a
   final weight `C − (2^m − 1)`. Carries the two proof obligations (sum = C,
   representability of every `t ≤ C`).

### Variables and decoding
3. **`AllocationQUBOVariable`** (define) — disjoint union of assignment
   variables `(p, n)` and storage variables `(n, chunk)`; must supply
   `Fintype`, `DecidableEq`, `LinearOrder` (see N1).
4. **`DecodeAssignment`** (define) — `AssignmentBit`/`StorageBit` accessors and
   the decoding `D_x(p) = {n | A_x(p,n) = 1}`, all in one artifact.

### Objective components (all ℝ-valued; cast before subtracting)
5. **`SafetyPenalty`** — `Σ_p (Σ_n A_x(p,n) − k)²`.
6. **`StoragePenalty`** — per-node squared gap between assigned size and
   selected chunk weight.
7. **`ProcessingObjective`** — `Σ_{p,n} cost·requests·(1 − A_x(p,n))`.
8. **`RemoteCostBound`** — `B_I = Σ_{p,n} cost·requests`; context also asks for
   `0 ≤ Q_C ≤ B_I` (see G1 below).
9. **`ValidPenaltyWeight`** — `B_I < h`.
10. **`AllocationObjective`** — `h·(Q_R + Q_S) + Q_C`.

### QUBO representation
11. **`AllocationObjectiveOffset`** — `K = h·|P|·k² + B_I`. *(Hand-checked:
    this is exactly the constant term of the expansion — `k²` per partition
    from `Q_R`, `B_I` from `Q_C`, nothing from `Q_S`.)*
12. **`allocationObjective_is_qubo`** (formalize) — ∃ q with
    `q.energy x + K = Q_{I,h}(x)` for all x. Likely the hardest single target:
    expanding squares and collecting coefficients under the variable order.
13. **`AllocationQUBO`** (define) — chosen witness, plus the named identity
    `allocationQUBO_energy_add_offset` that later proofs transfer minimizers
    through.

### Correctness
14. **`zeroPenaltyImpliesFeasible`** (formalize) — zero penalties ⇒ decoded
    assignment feasible.
15. **`feasibleAssignmentHasZeroPenaltyEncoding`** (formalize) — the converse
    encoding, storage bits from `binaryCapacityEncoding.encode`.
16. **`processingObjective_eq_processingCost`** (formalize) —
    `Q_C(x) = ↑(ProcessingCost I (DecodeAssignment I x))`.
17. **`quboEncodingCorrect`** (formalize) — the headline theorem: with
    `B_I < h` and a feasible assignment existing, QUBO minimizers decode
    exactly to optimal assignments (both directions).

After these, the file's closing note plans a further include for paper
Section 6 (knapsack reduction, variable counts, topology bounds) and warns that
the paper's displayed storage-connection bound omits a factor and must be
audited before being stated as a theorem.

### Math spot-checks (all pass)

- **Corrected encoding**: weights sum to `C`; final weight lies in
  `[1, 2^m]` so representability has no gap; the claimed examples check out
  (`C=7 → (1,2,4)`, `C=3 → (1,2)`), and the chunk count is `Nat.log2 C + 1`.
- **Penalty-weight argument**: penalties are squares of integers, so any
  violation costs ≥ `h > B_I`, while any feasible encoding costs
  `Q_C ≤ B_I` — the strict bound in `ValidPenaltyWeight` is exactly what
  target 17's proof sketch needs, and `h > B_I ≥ 0` rules out perverse
  negative weights.
- **Offset**: `E + K = Q` is consistent with diagonal-linear-term folding
  (`x² = x` on bits) under the `i ≤ j` energy convention.

## 5. Gaps to fix in the scaffold before/while formalizing

- **G1 — `RemoteCostBound` has a missing `uses` entry.** Its context says
  "Prove or expose that every ProcessingObjective lies between zero and this
  bound", and the LaTeX states `0 ≤ Q_C(x) ≤ B_I`, but
  `uses={AllocationInstance}` omits `ProcessingObjective`. With the recent
  targeted-imports prompting (commit `6758b71`), the run for this target may
  not import `ProcessingObjective` and the bound lemma would fail to
  elaborate. Fix: add `ProcessingObjective` to the `uses` list, or move the
  bound lemma into the `ValidPenaltyWeight`/`quboEncodingCorrect` targets.
- **G2 — empty `.lea/instructions.md`.** The prover reads it every run; the
  scaffold's conventions (cast-before-subtract, keep accessors in the
  DecodeAssignment artifact, instance-handling style per N1) currently live
  only in per-target `context` strings. Hoisting the global ones into
  instructions would make the 17 remaining runs more consistent.

None of the gaps affect the eight targets already completed.
