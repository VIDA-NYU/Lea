"""Parent collation — rank subagent candidates by the compiler, promote the winner (v2.3 item 25).

A coordinator run spawns N subagents; each returns a candidate `.lean` file and its
`lean_check` verdict (a `SubagentFinished` event). This module answers the one question
the coordinator must answer — *which candidate wins?* — and the answer is **not** a vote
and **not** the model's opinion. It is the compiler's:

    lean_check clean  >  SafeVerify-rejected  >  error / unchecked

ties broken by **sorry-free first, then the shorter proof, then a stable id**. The
candidate Lean says wins is the one that wins.

Two deliberate refusals encode the guardrails:

  * **Only a clean candidate is promotable.** A `lean_check` error never becomes the proof
    of record, and neither does a SafeVerify *rejection* — a rejection means "this compiled
    but cheated" (sorry-in-import, shadowing, axiom), and promoting a cheat to the canonical
    file is exactly the failure SafeVerify exists to catch. `select_promotable` returns only
    a tier-0 candidate; if nothing is clean it returns ``None`` and the coordinator keeps
    working rather than shipping a lie.
  * **Only the coordinator promotes.** A subagent physically cannot write the canonical file
    (D76 — its working dir is an ignored scratch tree that F3's sandbox confines it to), so
    `promote()` here is the single place a candidate crosses over into the proof of record.

SafeVerify is folded in **when available**: a candidate carries ``safeverify_status=None``
when the binary is not built, which keeps every candidate at the "clean" tier and degrades
ranking to lean_check-only — never a *mis*-rank. Ranking itself is a pure function of the
per-candidate verdicts; running SafeVerify to populate those verdicts is the caller's job
(the coordinator backend, item 24), so this module stays free of the SafeVerify subprocess
and is unit-testable without it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Ranking tiers — lower is better. The three the tracker names, in order.
_TIER_CLEAN = 0        # lean_check ok, and SafeVerify did not reject it
_TIER_SV_REJECTED = 1  # lean_check ok, but SafeVerify caught a cheat
_TIER_UNUSABLE = 2     # lean_check error / None (never compiled) — never promotable

# A final proof must be sorry-free (and axiom-free); a candidate carrying either is
# demoted within its tier so a clean-but-longer proof beats a short one with a hole.
_HOLE_RE = re.compile(r"(?<![\w.])(?:sorry|admit|axiom)(?![\w])")


@dataclass(frozen=True)
class Candidate:
    """One subagent's distilled candidate, as much of a `SubagentFinished` as ranking needs.

    `check_status` is the child's terminal `lean_check` verdict ('ok' | 'error' | None).
    `safeverify_status` is an optional SafeVerify audit of the *same* file
    ('ok' | 'rejected' | 'error' | 'unavailable' | None); ``None`` means "not run" and is
    treated as clean (we did not catch a cheat), never as a rejection. `text` is the
    candidate file's bytes (for the sorry-free / length tiebreak); ``None`` when unreadable.
    """

    result_id: str
    check_status: str | None
    text: str | None = None
    safeverify_status: str | None = None
    candidate_path: str | None = None

    # ---- derived ranking inputs ------------------------------------------------

    @property
    def tier(self) -> int:
        if self.check_status != "ok":
            return _TIER_UNUSABLE
        if self.safeverify_status == "rejected":
            return _TIER_SV_REJECTED
        return _TIER_CLEAN

    @property
    def has_hole(self) -> bool:
        """`sorry`/`admit`/`axiom` present — a proof with a hole, demoted within its tier."""
        return bool(self.text) and _HOLE_RE.search(self.text) is not None

    @property
    def proof_length(self) -> int:
        """Non-blank character count — the shorter-proof tiebreak. Blank lines and
        surrounding whitespace don't count, so formatting can't tip the ranking."""
        if not self.text:
            return 0
        return sum(len(line.strip()) for line in self.text.splitlines())

    @property
    def is_promotable(self) -> bool:
        """Only a tier-0 (compiled clean, not SafeVerify-rejected) candidate may become the
        proof of record. A hole (`sorry`/`axiom`) means it never compiled clean in the first
        place, so `check_status == 'ok'` already excludes it; the explicit guard is a belt."""
        return self.tier == _TIER_CLEAN and not self.has_hole


def _sort_key(c: Candidate) -> tuple:
    # tier first, then within a tier: hole-free beats hole, shorter beats longer, then a
    # stable tiebreak on result_id so ranking is total and deterministic (no coin-flips).
    return (c.tier, c.has_hole, c.proof_length, c.result_id)


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """Every candidate, best-first, by the compiler's verdict. A pure, total, deterministic
    order — same inputs, same ranking, every time (Python's sort is stable, and the
    result_id tiebreak makes the key total anyway)."""
    return sorted(candidates, key=_sort_key)


def select_winner(candidates: list[Candidate]) -> Candidate | None:
    """The best candidate overall for *reporting* ("3 candidates, #2 won") — may be an
    error-tier candidate if that's all there is. Not necessarily promotable; use
    `select_promotable` before writing anything to the canonical path."""
    ranked = rank(candidates)
    return ranked[0] if ranked else None


def select_promotable(candidates: list[Candidate]) -> Candidate | None:
    """The best candidate that is *safe to promote* — compiled clean and not SafeVerify
    rejected. ``None`` when nothing clean exists: the coordinator then keeps working (or
    reports no clean candidate) rather than promoting an error or a cheat."""
    for c in rank(candidates):
        if c.is_promotable:
            return c
    return None


@dataclass(frozen=True)
class Promotion:
    """The record of a promotion: which candidate won and the bytes now at the canonical
    path. `result_id` is the audit handle linking the resulting `code_step` back to the
    child transcript (item 22)."""

    result_id: str
    canonical_path: str
    content: str


def promote(winner: Candidate, canonical_path: str | Path) -> Promotion:
    """Write the winning candidate's bytes to the canonical proof path — the single crossing
    where a scratch candidate becomes the proof of record. Refuses a non-promotable winner
    (never compiled clean, or SafeVerify-rejected): promoting one would ship a proof the
    compiler already rejected.

    Returns the `Promotion` record; the caller records the `code_step` (with `result_id` as
    the provenance link). Storing the code_step is the store's job, kept out of here so this
    stays a pure filesystem + decision step.
    """
    if not winner.is_promotable:
        raise ValueError(
            f"refusing to promote candidate {winner.result_id!r}: not promotable "
            f"(check_status={winner.check_status!r}, safeverify={winner.safeverify_status!r}, "
            f"has_hole={winner.has_hole})"
        )
    if winner.text is None:
        raise ValueError(f"refusing to promote candidate {winner.result_id!r}: no content to write")
    path = Path(canonical_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(winner.text)
    return Promotion(result_id=winner.result_id, canonical_path=str(path), content=winner.text)


def candidate_from_event(event, *, base_dir: str | Path | None = None, safeverify_status: str | None = None) -> Candidate:
    """Build a `Candidate` from a `SubagentFinished`-shaped object (anything carrying
    `result_id` / `check_status` / `candidate_path`). Reads the candidate bytes off disk when
    `candidate_path` is resolvable — relative paths are resolved against `base_dir` (the run's
    working dir, where the child's scratch tree lives). An unreadable path yields ``text=None``
    (the candidate can still rank by verdict, just without the length/hole tiebreak)."""
    text: str | None = None
    rel = getattr(event, "candidate_path", None)
    if rel:
        p = Path(rel)
        if not p.is_absolute() and base_dir is not None:
            p = Path(base_dir) / p
        try:
            text = p.read_text()
        except OSError:
            text = None
    return Candidate(
        result_id=event.result_id,
        check_status=getattr(event, "check_status", None),
        text=text,
        safeverify_status=safeverify_status,
        candidate_path=rel,
    )
