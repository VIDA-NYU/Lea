import Mathlib

/-
Rehearsal proofs, independent of Lea's generated paths and namespaces.
Do not upload this file as part of the Overleaf project.
Do not replace a generated file wholesale: retain its imports and namespace.
The script gives proof-body edits and an explicit/implicit-binder repair exercise.
-/

namespace LeaDemoReference

def demo_double (n : Nat) : Nat := n + n

theorem demo_double_zero : demo_double 0 = 0 := by
  rfl

theorem demo_double_add (m n : Nat) :
    demo_double (m + n) = demo_double m + demo_double n := by
  simp only [demo_double]
  omega

-- Alternate proof body for the same statement: safe manual-edit example.
theorem demo_double_add_alternate (m n : Nat) :
    demo_double (m + n) = demo_double m + demo_double n := by
  simp only [demo_double]
  ring

theorem demo_double_two (n : Nat) : demo_double (n + 2) = demo_double n + 4 := by
  calc
    demo_double (n + 2) = demo_double n + demo_double 2 := demo_double_add n 2
    _ = demo_double n + 4 := by rfl

theorem demo_double_one : demo_double 1 = 2 := by
  rfl

theorem demo_nat_zero (n : Nat) : n + 0 = n := by
  rfl

theorem demo_square_bound (x : Real) (hx : 1 ≤ x) : x ≤ x ^ 2 := by
  have hx0 : 0 ≤ x := le_trans (by norm_num) hx
  have hprod : 0 ≤ x * (x - 1) := mul_nonneg hx0 (sub_nonneg.mpr hx)
  nlinarith

theorem original_square_claim_is_false : ¬ (∀ x : Real, x ≤ x ^ 2) := by
  intro h
  have hhalf := h (1 / 2)
  norm_num at hhalf

def demo_shifted_square (a x : Real) : Real := (x - a) ^ 2

theorem demo_shift_identity (a t : Real) :
    demo_shifted_square a (a + t) = t ^ 2 := by
  simp [demo_shifted_square]

theorem demo_shift_one (a : Real) : demo_shifted_square a (a + 1) = 1 := by
  calc
    demo_shifted_square a (a + 1) = (1 : Real) ^ 2 := demo_shift_identity a 1
    _ = 1 := by norm_num

theorem demo_shift_two (a : Real) : demo_shifted_square a (a + 2) = 4 := by
  calc
    demo_shifted_square a (a + 2) = (2 : Real) ^ 2 := demo_shift_identity a 2
    _ = 4 := by norm_num

end LeaDemoReference

namespace LeaDemoImplicitBinders

def demo_double (n : Nat) : Nat := n + n

-- Same mathematics, changed calling convention. Used only for the repair demo.
theorem demo_double_add {m n : Nat} :
    demo_double (m + n) = demo_double m + demo_double n := by
  simp only [demo_double]
  omega

-- Corrected consumer: the old `demo_double_add n 2` call would fail here.
theorem demo_double_two (n : Nat) : demo_double (n + 2) = demo_double n + 4 := by
  calc
    demo_double (n + 2) = demo_double n + demo_double 2 :=
      demo_double_add (m := n) (n := 2)
    _ = demo_double n + 4 := by rfl

end LeaDemoImplicitBinders

#print axioms LeaDemoReference.demo_double_zero
#print axioms LeaDemoReference.demo_double_add
#print axioms LeaDemoReference.demo_double_add_alternate
#print axioms LeaDemoReference.demo_double_two
#print axioms LeaDemoReference.demo_double_one
#print axioms LeaDemoReference.demo_nat_zero
#print axioms LeaDemoReference.demo_square_bound
#print axioms LeaDemoReference.original_square_claim_is_false
#print axioms LeaDemoReference.demo_shift_identity
#print axioms LeaDemoReference.demo_shift_one
#print axioms LeaDemoReference.demo_shift_two
#print axioms LeaDemoImplicitBinders.demo_double_add
#print axioms LeaDemoImplicitBinders.demo_double_two
