# Counterexample Workflows

## Summary

Lea should explicitly support counterexample-oriented mathematical workflows. A counterexample or proof of negation is a successful mathematical result, but it is not the same outcome as proving the original theorem true. The UI and Overleaf extension should make this distinction visible wherever run status, theorem status, or completion messaging is shown.

This feature introduces a clear product distinction between:

- proving the user's stated theorem, lemma, or conjecture;
- disproving the user's stated theorem, lemma, or conjecture by finding a counterexample or proving its negation.

## Problem

Lea can currently respond to a false theorem by proving the negation of the statement or by constructing a counterexample. This is mathematically useful, but the product can report the result using the same success language it would use for a verified proof of the original theorem.

That creates a misleading experience. A user may believe Lea has proven the theorem they entered, when Lea has actually shown that theorem to be false.

This distinction matters because counterexample construction is not just an error path. It is a common and valuable mathematical workflow. Users may intentionally ask Lea to disprove a conjecture, or Lea may discover that a theorem is false while attempting to prove it.

## Feature Goal

Lea should distinguish successful proof from successful disproof.

When Lea proves the user's stated theorem, the product should report ordinary proof success. When Lea proves that the user's stated theorem is false, the product should report a counterexample or disproof outcome. The latter should be treated as successful work, but never as a successful proof of the original claim.

## Core Principle

A counterexample is a successful mathematical result, but it is not a successful proof of the original theorem.

Lea's interface should preserve that distinction everywhere the result is displayed.

## User Workflows

### Case 1: Purposeful Counterexample Seeking

A user may provide a theorem, lemma, or conjecture that they know or suspect to be false, and ask Lea to find an explicit counterexample or otherwise disprove the statement.

In this case, Lea should treat the task as counterexample construction rather than ordinary theorem proving.

Expected behavior:

- Lea attempts to disprove the statement or construct a counterexample.
- If Lea succeeds, the result is presented as a verified disproof or counterexample.
- The final message makes clear that Lea has not proven the original statement true.
- The status shown in the UI and Overleaf extension is distinct from ordinary proof success.

Example user-facing language:

> Lea found a verified counterexample to the proposed statement. This means the original theorem was disproven, not proven.

Possible status labels:

- `Counterexample found`
- `Statement disproven`
- `Disproof verified`

### Case 2: Accidental Counterexample Proof

A user may ask Lea to prove a theorem that is false. During the attempt, Lea may discover a counterexample or prove the negation of the theorem.

In this case, Lea should surface the result as valuable mathematical progress while clearly communicating that the original theorem was not proven.

Expected behavior:

- Lea may complete the run by proving the statement false.
- The result is not shown as ordinary proof success.
- The user can immediately see that Lea disproved the original statement.
- The final message explains that Lea found a counterexample or verified the negation.

Example user-facing language:

> Lea was unable to prove the theorem as stated because it found a verified counterexample. The result shows that the original statement is false.

Possible status labels:

- `Disproved`
- `Counterexample found`
- `False statement detected`

## Desired Status Model

The product should represent proof and disproof as separate result states.

A possible status taxonomy:

- `proved`: the stated theorem was verified.
- `disproved`: the stated theorem was shown false by counterexample or proof of negation.
- `failed`: Lea did not complete a proof or disproof.
- `needs_review`: Lea produced partial, ambiguous, or uncertain work.
- `running`: Lea is still working.

The exact internal naming can differ, but the user-facing distinction between `proved` and `disproved` should be preserved.

## Standalone UI Requirements

The standalone UI should make counterexample outcomes visible anywhere a user would otherwise see proof success.

This includes:

- run completion banners;
- session or theorem status labels;
- timeline and result history views;
- final assistant messages;
- code canvas or proof metadata, where applicable.

The user should not need to inspect the Lean code in detail to determine whether Lea proved the original theorem or disproved it.

## Overleaf Extension Requirements

The Overleaf extension should introduce a distinct label for counterexample or disproof outcomes.

A theorem block whose statement has been disproven should not receive the same visual treatment as a verified theorem.

Possible Overleaf labels:

- `Disproved`
- `Counterexample`
- `Counterexample found`

The label should be visibly distinct from ordinary proof success, using copy, color, iconography, or some combination of those cues.

## Messaging Requirements

User-facing language should avoid implying that a false theorem was proven true.

In purposeful counterexample mode, Lea should acknowledge that the user asked for a counterexample and report success in those terms.

In accidental counterexample mode, Lea should explicitly state that the original theorem was not proven and that the result instead demonstrates falsehood.

Preferred language should be direct and mathematically precise:

- "Lea found a counterexample."
- "Lea disproved the statement as written."
- "The original theorem was not proven."
- "The verified result shows the statement is false."

Avoid ambiguous success language such as:

- "Proof complete" when the original theorem was disproven.
- "The theorem was verified" when Lea verified only the negation.
- "Success" without explaining whether the success was proof or disproof.

## Acceptance Criteria

- If the user explicitly asks for a counterexample and Lea succeeds, the final state is shown as a counterexample or disproof outcome, not ordinary proof success.
- If the user asks for a proof but Lea proves the negation instead, the final state clearly says that the original statement was disproven.
- The standalone UI has distinct copy and status presentation for counterexample outcomes.
- The Overleaf extension has a distinct visual/status label for counterexample outcomes.
- Historical or session views preserve the distinction between proof and disproof outcomes.
- The user-facing language never implies that Lea proved a statement true when Lea actually proved it false.

## Open Product Questions

- Should purposeful counterexample seeking be a user-selectable mode, inferred from the prompt, or both?
- Should `disproved` and `counterexample found` be separate statuses, or should one be the canonical status with the other used as explanatory copy?
- How should Lea report cases where it suspects a counterexample but has not produced a verified Lean artifact?
- Should the UI show the original theorem and the verified negation side by side when a disproof is found?
- Should Overleaf insert or annotate the counterexample result differently from an ordinary formalized theorem?
