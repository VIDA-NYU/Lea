namespace Lea.RetryLab

-- KEEP: handwritten context, ticket 731.
def kept_def : Nat := 7

theorem kept_a (n : Nat) : n + 0 = n := by
  exact Nat.add_zero n

private theorem hidden_helper (n : Nat) : n + 0 = n := by
  sorry

theorem retry_b (n : Nat) : n + 0 = n := by
  exact hidden_helper n

-- KEEP: unrelated work must remain unfinished.
theorem untouched_c (n : Nat) : n + 0 = n := by
  sorry

end Lea.RetryLab
