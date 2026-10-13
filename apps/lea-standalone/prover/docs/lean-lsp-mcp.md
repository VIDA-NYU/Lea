# Using the `lean-lsp-mcp` server and a Lean skill

This guide shows how to give Lea two config-driven capabilities at once:

- **An MCP server** — [`lean-lsp-mcp`](https://github.com/oOo0oOo/lean-lsp-mcp), the
  Lean theorem-prover MCP that bridges an LLM to the Lean 4 **Language Server**
  (goals, diagnostics, hover, and Mathlib search). It's the Lean backbone of
  [Numina-Lean-Agent](https://github.com/project-numina/numina-lean-agent).
- **A skill** — a markdown fragment of procedural knowledge that tells the model
  *how* to drive those tools.

Both are pure configuration. Nothing about Lea's core loop changes: the MCP
server's tools register into the same tool registry as the built-ins, and the
skill is appended to the system prompt.

> Background on the mechanisms: tool registry → `docs/decisions.md` decision 9,
> skills → decision 10, MCP → decision 11.

---

## Prerequisites

- **`uv` / `uvx`** on your `PATH` (you already use `uv` to run Lea). `uvx` fetches
  and runs `lean-lsp-mcp` on demand — no manual install needed.
- **A Lean 4 project with Mathlib built**, e.g. this repo's `workspace/`
  (`cd workspace && lake exe cache get && lake build`). `lean-lsp-mcp` starts a
  Lean language server against this project, so the first tool call warms Mathlib
  and can take a while.
- A model API key in the environment (e.g. `GEMINI_API_KEY`).

---

## 1. Enable the MCP server

Add an `mcp.servers` entry. `lean-lsp-mcp` is a **stdio** server launched via
`uvx`; point it at your Lean project with `LEAN_PROJECT_PATH`:

```yaml
# configs/lean-lsp.yaml
mcp:
  servers:
    lean-lsp:
      command: uvx
      args: [lean-lsp-mcp]
      env:
        # Absolute path to a Lean project root (has lakefile + built Mathlib).
        LEAN_PROJECT_PATH: /ABSOLUTE/PATH/TO/lea-prover/workspace
```

Notes:

- **`env` is merged over your real environment**, so setting only
  `LEAN_PROJECT_PATH` still leaves `PATH` intact (needed to find `uvx`/`lake`).
- **Failure is non-fatal.** If the server can't start, Lea prints a `[mcp]`
  warning and continues with the remaining tools (it won't abort your run).
- **Remote servers** use `url:` instead of `command:` (streamable HTTP by
  default, or `transport: sse`) — not needed for `lean-lsp-mcp`.

### Tools it adds

On startup Lea connects, lists the server's tools, and registers them. The tools
keep their **real names** (e.g. `lean_goal`) — they're only prefixed with
`lean-lsp__` if a name collides with another tool. The headline tools:

| Tool | What it does |
|---|---|
| `lean_goal` | Proof goals at a position — *the* most-used tool |
| `lean_run_code` | Compile a self-contained snippet (must include imports), return diagnostics |
| `lean_diagnostic_messages` | Errors/warnings/infos for a file |
| `lean_multi_attempt` | Try several tactics at once, get each resulting goal state |
| `lean_hover_info` | Type signature + docs for a symbol |
| `lean_local_search` | Fast check that a declaration exists (before you use it) |
| `lean_leansearch` / `lean_loogle` / `lean_state_search` / `lean_hammer_premise` | Mathlib search by NL / type / goal / premise (remote; rate-limited) |

(There are ~22 in total — also `lean_completions`, `lean_file_outline`,
`lean_term_goal`, `lean_declaration_file`, `lean_references`, `lean_build`,
`lean_verify`, `lean_code_actions`, `lean_run_code`, widget/profiling tools.)

The remote search tools (`lean_leansearch`, `lean_loogle`, …) call public
endpoints and are rate-limited (e.g. leansearch 3 req / 30 s); the LSP tools are
local.

### Run it

```bash
uv run lea --config configs/lean-lsp.yaml \
  "Prove that the sum of two even naturals is even."
```

The model can now call `lean_goal`, `lean_run_code`, `lean_loogle`, etc. directly.

#### Optional: restrict the toolset

By default the model sees **all** tools (the built-ins + every MCP tool). To
hand it a focused set, use the `agent.tools` allowlist (names are matched exactly,
order = the order shown to the model):

```yaml
agent:
  tools: [write_file, lean_run_code, lean_goal, lean_loogle, lean_local_search]
```

---

## 2. Add a skill

A skill is just a markdown file listed in `agent.skills`; its content is appended
to the system prompt (in order). Use one to teach the model *how* to use the
`lean-lsp-mcp` tools well.

Create `skills/lean_lsp_proving.md`:

```markdown
# Skill: proving with the Lean LSP tools

You have live Lean language-server tools. Use them instead of guessing:

- Start from the goal: call `lean_goal` at the proof position to see exactly
  what must be proved. Re-check it after every tactic.
- Before naming a lemma, confirm it exists with `lean_local_search`; to discover
  lemmas, use `lean_loogle` (by type signature) or `lean_leansearch` (natural
  language). Respect their rate limits — search deliberately, not in a loop.
- To trial tactics without editing the file, use `lean_multi_attempt` and read
  the resulting goal states; keep the one that closes or simplifies the goal.
- Verify a complete attempt with `lean_run_code` (include all `import`s). The
  proof is done only when its `diagnostics` array is empty.
- Prefer small, checkable steps. After each change, look at diagnostics before
  moving on.
```

Reference it in config:

```yaml
agent:
  skills: [skills/lean_lsp_proving.md]
```

Skill paths are resolved relative to your current working directory. A missing
file is a hard error (so a typo is caught immediately, not silently ignored).

---

## 3. Full example config

A complete config combining the model, the skill, and the MCP server:

```yaml
# configs/lean-lsp.yaml
model:
  name: gemini/gemini-3.1-pro-preview
  stream: true
  model_kwargs:
    max_tokens: 16384

agent:
  prompt_variant: default
  max_turns: null
  skills: [skills/lean_lsp_proving.md]
  # tools: null  → all built-ins + all lean-lsp tools (default)

mcp:
  servers:
    lean-lsp:
      command: uvx
      args: [lean-lsp-mcp]
      env:
        LEAN_PROJECT_PATH: /ABSOLUTE/PATH/TO/lea-prover/workspace
```

Run:

```bash
uv run lea --config configs/lean-lsp.yaml "Prove that 2 + 2 = 4."
```

You'll see the model call the Lean LSP tools (`-> lean_run_code(...)`,
`-> lean_goal(...)`) and the tool results inline, with per-turn token/cost.

---

## Troubleshooting

- **`[mcp] server 'lean-lsp' failed to start`** — check `uvx` is on `PATH` and
  `LEAN_PROJECT_PATH` is an absolute path to a real Lean project. The run
  continues without the server's tools.
- **First call is very slow / times out** — the Lean server is loading Mathlib.
  Make sure the project is pre-built (`lake build` / `lake exe cache get`).
- **Model calls an unknown tool** — list the active tools by running once and
  reading the registered names, or pin them with `agent.tools`.
- **Search tool errors / throttling** — the remote search tools are rate-limited;
  avoid calling them in tight loops (the skill above tells the model this).
