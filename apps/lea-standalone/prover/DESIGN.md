# Lea — Design Document

A minimal Lean 4 theorem proving agent, inspired by [pi](https://github.com/badlogic/pi-mono).

## Philosophy

Lea follows Pi's ethos of radical minimalism: if we don't need it, we don't build it. The agent should be transparent, observable, and simple enough to understand in a single sitting.

- **Minimal tools**: the smallest set of tools that lets an LLM write and verify Lean proofs.
- **Full observability**: every tool call, result, and model response is visible. No hidden orchestration.
- **Trust over guardrails**: no permission prompts. The agent has full access to files and shell.
- **Simple prompts**: frontier models already know how to be coding agents. Keep the system prompt short.
- **Collaborator, not oracle**: Lea is a tool for mathematicians, not a replacement. Legibility, insight, and the ability to intervene matter as much as raw solve rate.

## Architecture

```
User task (CLI) → agent loop → tool calls → Lean compilation → repeat until proof compiles
```

### Built-in tools

| Tool | Purpose |
|------|---------|
| `read_file` | Read file contents |
| `write_file` | Create or overwrite a file |
| `edit_file` | Replace an exact substring in a file |
| `lean_check` | Compile a `.lean` file via `lake env lean`, return diagnostics |
| `search_mathlib` | Grep Mathlib source for lemma names / type patterns |
| `suggest_imports` | Suggest a targeted import block with Mathlib's min-imports linter |
| `bash` | Run a shell command (for `exact?`, `apply?`, `lake build`, etc.) |

### Implemented features

1. **Streaming output** — model output streams in real time. Every tool call and result is visible as it happens.
2. **Multi-provider support** — Gemini, Anthropic, OpenAI via a thin provider abstraction in `providers.py`. Auto-detected from model name or set with `-p`. OpenAI `-pro` reasoning models route through the Responses API.
3. **No default turn limit** — the agent runs until the model stops calling tools. `--max-turns` available as an optional safety valve.
4. **Bash tool** — the agent can run arbitrary shell commands, enabling `exact?`, `apply?`, `grep`, `lake build`, etc.
5. **Project-level prompt customization** — drop a `lea.md` file in the workspace to append project-specific instructions to the system prompt.
6. **Session persistence** — full conversation history saved to `~/.lea/sessions/` after each run. Resume with `--resume`.
7. **Cost and token tracking** — cumulative input/output tokens and estimated cost printed at the end of each run.
8. **Phase modes for collaborator workflow** — `--sketch` and `--fill` swap in phase-specific system prompts. Useful when a mathematician wants to inspect a skeleton before fill, or fill a single sorry in an existing file. The agent loop is unchanged; only the system prompt varies.

### Evaluation

Eval harnesses (miniF2F, Putnam, FormalQualBench best-of-n) live in the separate
**Lea + eval repo**, not in this UI-vendored copy. The agent core is the product; the
CLI, the UI, and eval are separate *consumers* of the same loop — so the benchmark
suite is maintained alongside upstream Lea rather than bundled here.

## Limitations

The single-loop architecture works well on competition math where proofs are short. On graduate-level math (FQB), three patterns persist:

- **No proof structure.** The agent often attempts the entire proof in one shot. For theorems requiring intermediate lemmas, it produces a monolith or gets lost.
- **Single strategy.** Every problem gets the same approach: try simple tactics, then search Mathlib. There's no mechanism to try fundamentally different proof strategies.
- **Cheats when stuck.** When a proof path isn't visible, the agent reaches for namespace shadowing, import-sorry, or empty-file tricks. Verification rigor (SafeVerify, Comparator) is the durable fix.


## Influences

- **[pi-mono](https://github.com/badlogic/pi-mono)** — radical-minimalism stance. Lea is a Lean-flavored fork of Pi's coding-agent shape.
- **[DeltaProver](https://arxiv.org/html/2507.15225)**, **[DeepSeek-Prover-V2](https://arxiv.org/html/2504.21801v1)** — sketch-fill-reflect framing. Lea exposes phase modes for collaborator workflow but does *not* orchestrate them; an earlier `prove_hard` state machine was tried and removed.
- **Armstrong–Kempe (De Giorgi-Nash-Moser formalization)** — blueprints drive everything; file decomposition is the architecture; supervision over autonomy.
