# Lea

A minimal Lean 4 theorem proving agent, inspired by [pi](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent).

Lea translates natural-language math statements into Lean 4 proofs that compile with zero errors and zero `sorry`s.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and at least one API key.

```bash
# Install elan (Lean version manager — provides lean, lake, etc.)
curl https://elan.lean-lang.org/elan-init.sh -sSf | sh

# Set your API key
export GOOGLE_API_KEY=...     # for Gemini models (default)
export ANTHROPIC_API_KEY=...  # for Claude models
export OPENAI_API_KEY=...     # for GPT/o-series models

# Build the Lean workspace (downloads Mathlib — takes a while the first time)
cd workspace && lake build && cd ..

# That's it. Run:
uv run lea "Prove that the square root of 2 is irrational"
```

## Example

Define the Ackermann function, prove it's strictly monotone and grows faster than its argument, and compute `ackermann 4 1 = 65533`:

```bash
uv run lea "Define the Ackermann function using well-founded recursion. Prove that \
  for all m n, ackermann m n > n. Prove that for all m n, \
  ackermann m (n+1) > ackermann m n. Prove ackermann 4 1 = 65533. Do not use Mathlib."
```

See [examples/](examples/) for generated proofs.

## How it works

Lea runs a simple loop:

1. Write a `.lean` file with a first-attempt proof
2. Compile with `lean_check`
3. If it compiles — done. If not — read the errors, edit, retry.
4. If stuck, search Mathlib for relevant lemmas, or use `bash` to explore.

Built-in tools: `read_file`, `write_file`, `edit_file`, `lean_check`,
`search_mathlib`, `suggest_imports`, and `bash`. Supports Gemini, Anthropic,
and OpenAI models. See [USAGE.md](USAGE.md) for full CLI reference.

## Eval results

Evaluation harnesses (miniF2F, Putnam, FormalQualBench best-of-n) and their results live in the separate **Lea + eval repo**. This is the UI-vendored copy of the agent — it does not carry the benchmark suite, keeping it lean for serving the LeaUI interface.

## Customization

Drop a `lea.md` file in your working directory or workspace root to add project-specific instructions to the system prompt (preferred tactics, import conventions, etc.).
