# Blueprint — Determine all positive integers k with (n+1) | sum_i C(n,i)^k for all n

Answer: exactly the **even** positive integers k.

## Goal

```lean
theorem sum_choose_pow_dvd_iff (k : ℕ) (hk : 0 < k) :
    (∀ n : ℕ, 0 < n →
      (n + 1 : ℕ) ∣ ∑ i in Finset.range (n + 1), (n.choose i) ^ k) ↔ Even k
```

Everything below is phrased with m = n + 1 (so m ≥ 2) and the sum T(k,m) = Σ_{i=0}^{m-1} C(m-1,i)^k;
the condition reads "m ∣ T(k,m) for every m ≥ 2".

## sum_choose_pow_dvd_iff
- kind: theorem
- uses: even_imp_dvd, odd_imp_not_dvd

For every positive integer k, (n+1) divides Σ_{i=0}^{n} C(n,i)^k for all n ≥ 1 if and only if k is even; so the answer is "k even".

## even_imp_dvd
- kind: theorem
- uses: dvd_of_prime_pow_dvd, prime_pow_dvd

For even k and every m ≥ 2, m divides T(k,m); proved prime-by-prime using the factorization of m.

## odd_imp_not_dvd
- kind: theorem
- uses: T_three, three_not_dvd_two_add_two_pow

For odd k the condition fails already at n = 2: since C(2,0)=C(2,2)=1 and C(2,1)=2 we have T(k,3) = 2 + 2^k, which is not divisible by 3.

## dvd_of_prime_pow_dvd
- kind: lemma
- uses: sumChoosePow

If p ^ v_p(m) divides T(k,m) for every prime p dividing m, then m divides T(k,m), because m is the product of its pairwise coprime prime-power parts.

## prime_pow_dvd
- kind: theorem
- uses: sum_eq_unit_pow, unit_one_add_delta, binom_expand_one_add_delta, delta_pow_sum_vanishes

For a prime p, a ≥ 1, even k and any m with p^a ∣ m, we have p^a ∣ T(k,m). This is the technical heart of the problem and the main risk in the whole plan: the prime case m = p is immediate, but passing to p^a ∣ m needs genuine p-adic cancellation.

## sum_eq_unit_pow
- kind: lemma
- uses: choose_eq_sign_prod, sign_factor_pow_even

For even k, T(k,m) = Σ_{i=0}^{m-1} u_i^k as rational numbers, where u_i = Π_{j=1}^{i} (1 - m/j); this removes all the signs, which is exactly where the hypothesis "k even" is used.

## choose_eq_sign_prod
- kind: lemma
- uses: none

For 0 ≤ i ≤ m-1, C(m-1,i) = (-1)^i · Π_{j=1}^{i} (1 - m/j), an identity in ℚ obtained by factoring (m-j) = -j(1 - m/j) in each factor of C(m-1,i) = Π_{j=1}^{i} (m-j)/j.

## sign_factor_pow_even
- kind: lemma
- uses: none

If k is even and c^k = 1 then (c·u)^k = u^k; applied with c = (-1)^i, using that i·k is even.

## unit_one_add_delta
- kind: lemma
- uses: delta_valuation

For p prime with p^a ∣ m and i < m, writing u_i = 1 + δ_i, the value u_i is a p-adic unit (v_p(u_i) = 0) and v_p(δ_i) ≥ 1; hence u_i ≡ 1 (mod p) for every i.

## delta_valuation
- kind: lemma
- uses: none

Valuation estimate for the individual factors: if p^a ∣ m and j < m then v_p(1 - m/j) ≥ a - v_p(j) ≥ 1, and consequently v_p(δ_i) = v_p(u_i - 1) ≥ a - v_p(i!).

## binom_expand_one_add_delta
- kind: lemma
- uses: none

Binomial expansion (1 + δ)^k = Σ_{ℓ=0}^{k} C(k,ℓ) · δ^ℓ for every k, so Σ_i u_i^k = Σ_{ℓ=0}^{k} C(k,ℓ) Σ_i δ_i^ℓ; the ℓ = 0 term is exactly m, which is where the factor p^a comes from.

## delta_pow_sum_vanishes
- kind: lemma
- uses: delta_valuation, block_cancellation

For every ℓ ≥ 1 the sum Σ_{i=0}^{m-1} δ_i^ℓ is divisible by a power of p large enough that C(k,ℓ) · Σ_i δ_i^ℓ is divisible by p^a; combined with the ℓ = 0 term m this gives p^a ∣ T(k,m).

## block_cancellation
- kind: lemma
- uses: delta_periodicity

The cancellation step: summing δ_i^ℓ over a complete block of p^a consecutive i's produces a p-adically small total because only the leading order of δ_i survives, and the surviving terms ±1-cancel.

## delta_periodicity
- kind: lemma
- uses: delta_valuation

HIGH RISK NODE: δ_i depends on i only through a bounded amount of p-adic information (δ_i ≡ 0 mod p^{a - v_p(i!)}), which licenses grouping the indices into complete residue blocks modulo p^a for the block-cancellation argument.

## T_three
- kind: lemma
- uses: sumChoosePow

T(k,3) = 1^k + 2^k + 1^k = 2 + 2^k, a direct evaluation of the three binomial coefficients of row 2.

## three_not_dvd_two_add_two_pow
- kind: lemma
- uses: two_pow_mod_three

If k is odd then 2 + 2^k ≡ 2 + (-1)^k = 1 (mod 3), so 3 does not divide 2 + 2^k.

## two_pow_mod_three
- kind: lemma
- uses: none

For every k, 2^k ≡ (-1)^k (mod 3), since 2 ≡ -1 modulo 3.

## sumChoosePow
- kind: definition
- lean: `Lea.RealAnalysis.sumChoosePow` — file `Lea/RealAnalysis/SumChoosePow.lean`
- status: PROVED (lean_check clean: no errors, no warnings, no `sorry`)
- uses: none

T(k,m) = Σ_{i ∈ Finset.range m} (m - 1).choose i ^ k, the normalized sum with m = n + 1, so that the target condition becomes "m ∣ T(k,m) for all m ≥ 2".

Companion lemmas proved alongside the definition (in the same file):
- `sumChoosePow_succ (k n) : sumChoosePow k (n+1) = ∑ i ∈ Finset.range (n+1), n.choose i ^ k` — `rfl`; the reindexing bridge `m = n+1`.
- `@[simp] sumChoosePow_one (k) : sumChoosePow k 1 = 1`
- `@[simp] sumChoosePow_zero (k) : sumChoosePow k 0 = 0`

Note: the sum binder must be written `∑ i ∈ s, f i`; the Lean-3 spelling `∑ i in s, f i` does not parse in this Mathlib version.
