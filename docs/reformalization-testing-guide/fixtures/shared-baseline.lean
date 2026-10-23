namespace Lea.RetryLab

-- KEEP: handwritten context, ticket 731.
def kept_def : Nat := 7

theorem kept_a (n : Nat) : n + 0 = n := by
  exact Nat.add_zero n

theorem retry_b (n : Nat) : n + 0 = n := by
  have hn : n = n := by rfl
  sorry

-- KEEP: unrelated work must remain unfinished.
theorem untouched_c (n : Nat) : n + 0 = n := by
  sorry

end Lea.RetryLab
