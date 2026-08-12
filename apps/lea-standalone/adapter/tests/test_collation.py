"""Parent collation (v2.3 item 25) — the compiler decides, not a vote.

These pin the ranking as a *total order by the compiler* and the two refusals that keep a
bad candidate off the canonical path:

  * tier order: lean_check clean > SafeVerify-rejected > error/unchecked;
  * tiebreak within a tier: sorry-free first, then shorter proof, then a stable id;
  * SafeVerify absent (None) ranks as clean — degrade to lean_check-only, never mis-rank;
  * `select_promotable` returns ONLY a clean candidate (never an error, never a cheat);
  * `promote` writes the winner's bytes to the canonical path and refuses a non-promotable
    one — the single crossing where a scratch candidate becomes the proof of record;
  * `candidate_from_event` reads the child's candidate bytes relative to the run's dir.
"""

from dataclasses import dataclass

import pytest

from app.collation import (
    Candidate,
    Promotion,
    candidate_from_event,
    promote,
    rank,
    select_promotable,
    select_winner,
)

_OK = "theorem t : True := by trivial\n"
_OK_LONG = "theorem t : True := by\n  have h : True := trivial\n  exact h\n"
_HOLE = "theorem t : True := by sorry\n"


def _c(result_id, check_status, *, text=None, sv=None):
    return Candidate(result_id=result_id, check_status=check_status, text=text, safeverify_status=sv)


# --- tier ordering -------------------------------------------------------------

def test_clean_beats_sv_rejected_beats_error():
    err = _c("err", "error", text=None)
    rejected = _c("rej", "ok", text=_OK, sv="rejected")
    clean = _c("win", "ok", text=_OK, sv="ok")
    order = [c.result_id for c in rank([err, rejected, clean])]
    assert order == ["win", "rej", "err"]


def test_safeverify_none_ranks_as_clean():
    # No SafeVerify binary → status None on every candidate → they stay at the clean tier
    # and rank by lean_check + tiebreak, not demoted to the rejected tier.
    a = _c("a", "ok", text=_OK, sv=None)
    b = _c("b", "error", text=None, sv=None)
    assert a.tier == 0 and b.tier == 2
    assert select_winner([b, a]).result_id == "a"


def test_unchecked_status_is_unusable_tier():
    assert _c("u", None).tier == 2


# --- tiebreaks within a tier ---------------------------------------------------

def test_sorry_free_beats_hole_within_tier():
    # Both are the same tier by lean_check status; the hole loses. (A real 'ok' proof
    # can't contain sorry, but the guard must hold if a verdict is ever stale/mismatched.)
    hole = _c("hole", "ok", text=_HOLE)
    clean = _c("clean", "ok", text=_OK)
    assert clean.has_hole is False and hole.has_hole is True
    assert select_winner([hole, clean]).result_id == "clean"


def test_shorter_proof_wins_the_tiebreak():
    short = _c("short", "ok", text=_OK)
    longer = _c("long", "ok", text=_OK_LONG)
    assert short.proof_length < longer.proof_length
    assert select_winner([longer, short]).result_id == "short"


def test_length_ignores_blank_lines_and_whitespace():
    padded = _c("padded", "ok", text="\n\n" + _OK + "   \n\n")
    tight = _c("tight", "ok", text=_OK)
    assert padded.proof_length == tight.proof_length


def test_ranking_is_stable_and_deterministic():
    # Identical verdicts + identical text → order decided by result_id, every time.
    xs = [_c("c", "ok", text=_OK), _c("a", "ok", text=_OK), _c("b", "ok", text=_OK)]
    assert [c.result_id for c in rank(xs)] == ["a", "b", "c"]
    assert [c.result_id for c in rank(list(reversed(xs)))] == ["a", "b", "c"]


def test_axiom_counts_as_a_hole():
    axiomd = _c("ax", "ok", text="axiom cheat : True\ntheorem t : True := cheat\n")
    clean = _c("ok", "ok", text=_OK)
    assert axiomd.has_hole is True
    assert select_winner([axiomd, clean]).result_id == "ok"


def test_sorry_substring_does_not_falsely_trigger():
    # 'sorry' as part of an identifier must not read as a hole (word boundaries).
    assert _c("suffix", "ok", text="def sorryless : Nat := 0\ntheorem t : True := trivial\n").has_hole is False
    assert _c("prefix", "ok", text="theorem no_sorry_here : True := by trivial\n").has_hole is False
    assert _c("real", "ok", text=_HOLE).has_hole is True


# --- promotable selection ------------------------------------------------------

def test_select_promotable_skips_error_and_rejected():
    err = _c("err", "error")
    rejected = _c("rej", "ok", text=_OK, sv="rejected")
    clean = _c("clean", "ok", text=_OK, sv="ok")
    assert select_promotable([err, rejected, clean]).result_id == "clean"


def test_select_promotable_none_when_nothing_clean():
    assert select_promotable([_c("err", "error"), _c("rej", "ok", text=_OK, sv="rejected")]) is None
    assert select_promotable([]) is None


def test_winner_may_be_unusable_but_promotable_is_not():
    # select_winner reports the best of a bad lot (for the UI); select_promotable refuses it.
    only_errors = [_c("e1", "error"), _c("e2", "error")]
    assert select_winner(only_errors) is not None
    assert select_promotable(only_errors) is None


# --- promotion (the one crossing to proof-of-record) ---------------------------

def test_promote_writes_canonical_and_returns_record(tmp_path):
    winner = _c("premise-search-abc", "ok", text=_OK, sv="ok")
    canonical = tmp_path / "proofs" / "T.lean"
    result = promote(winner, canonical)
    assert isinstance(result, Promotion)
    assert canonical.read_text() == _OK
    assert result.result_id == "premise-search-abc"          # the audit handle survives
    assert result.canonical_path == str(canonical)
    assert result.content == _OK


def test_promote_creates_missing_parent_dirs(tmp_path):
    promote(_c("w", "ok", text=_OK), tmp_path / "a" / "b" / "c" / "P.lean")
    assert (tmp_path / "a" / "b" / "c" / "P.lean").exists()


def test_promote_refuses_a_non_compiling_winner(tmp_path):
    with pytest.raises(ValueError, match="not promotable"):
        promote(_c("bad", "error", text="theorem t : True := by\n"), tmp_path / "x.lean")


def test_promote_refuses_a_safeverify_rejected_winner(tmp_path):
    with pytest.raises(ValueError, match="not promotable"):
        promote(_c("cheat", "ok", text=_OK, sv="rejected"), tmp_path / "x.lean")


def test_promote_refuses_a_holey_winner(tmp_path):
    with pytest.raises(ValueError, match="not promotable"):
        promote(_c("holey", "ok", text=_HOLE), tmp_path / "x.lean")


def test_promote_refuses_when_no_content(tmp_path):
    with pytest.raises(ValueError, match="no content"):
        promote(Candidate(result_id="w", check_status="ok", text=None), tmp_path / "x.lean")


# --- candidate_from_event ------------------------------------------------------

@dataclass
class _FakeEvent:
    result_id: str
    check_status: str | None
    candidate_path: str | None


def test_candidate_from_event_reads_relative_to_base_dir(tmp_path):
    scratch = tmp_path / ".lea" / "tmp" / "run" / "a"
    scratch.mkdir(parents=True)
    (scratch / "C.lean").write_text(_OK)
    ev = _FakeEvent(result_id="a-1", check_status="ok", candidate_path=".lea/tmp/run/a/C.lean")
    c = candidate_from_event(ev, base_dir=tmp_path, safeverify_status="ok")
    assert c.result_id == "a-1" and c.check_status == "ok"
    assert c.text == _OK and c.safeverify_status == "ok"
    assert c.is_promotable


def test_candidate_from_event_tolerates_unreadable_path(tmp_path):
    ev = _FakeEvent(result_id="gone", check_status="ok", candidate_path="nope/missing.lean")
    c = candidate_from_event(ev, base_dir=tmp_path)
    assert c.text is None                 # unreadable → text None, but still ranks by verdict
    assert c.check_status == "ok"


def test_candidate_from_event_handles_no_candidate_path(tmp_path):
    ev = _FakeEvent(result_id="none", check_status="error", candidate_path=None)
    c = candidate_from_event(ev, base_dir=tmp_path)
    assert c.text is None and c.tier == 2
