"""The curated capability catalog (v2.5 E1/E2, A6).

One entry = one thing a mathematician can install without knowing what MCP is: a server,
the tools worth exposing from it, and — where one exists — the skill that teaches their
use. That bundle is the unit, not the server alone: measured, `lean-lsp-mcp` adds 23 tools
that overlap Lea's own, and a live run confirmed the agent then reaches for the familiar
built-in and never inspects a goal. A server without its skill makes the prover *worse*.

**Versions are pinned (A6).** `uvx <pkg>` downloads and executes the latest release from
PyPI at every run start. That — not privilege escalation — is the real exposure here, and
pinning is what makes a curated entry a fixed, auditable thing rather than a moving one.

Kept deliberately small. A general-purpose harness cannot curate (it would need to cover
every domain, forever); a Lean prover has perhaps three entries, and that narrowness is
exactly what makes curation affordable.
"""

from __future__ import annotations

# Pinned to the version actually measured: 23 tools, 0.6s start, no required env.
LEAN_LSP_VERSION = "0.29.0"

CATALOG: list[dict] = [
    {
        "id": "lean-lsp",
        "title": "Lean language server",
        "summary": "Lets Lea inspect proof goals, read errors, and search Mathlib the way "
                   "the Lean editor does.",
        "requires": "Needs `uvx` (installed with Lea) and a built Lean project.",
        "server": {
            "name": "Lean LSP",
            "transport": "stdio",
            "command": "uvx",
            # A6: pinned. An unpinned `uvx lean-lsp-mcp` executes whatever PyPI serves
            # that day.
            "args": [f"lean-lsp-mcp=={LEAN_LSP_VERSION}"],
            # `LEAN_PROJECT_PATH` is filled in from the machine at install time — it is
            # the one field a user cannot be expected to know.
            "needs_lean_path": True,
        },
        # The overlap fix: expose the tools that ADD something, not all 23. `lean_goal`
        # and friends have no Lea equivalent; `lean_leansearch`/`lean_loogle` duplicate
        # `search_mathlib` and are what the agent reaches for by mistake.
        "recommended_tools": [
            "lean_goal", "lean_term_goal", "lean_hover_info", "lean_diagnostic_messages",
            "lean_multi_attempt", "lean_declaration_file", "lean_local_search",
        ],
        "skill_url": "https://github.com/cameronfreer/lean4-skills",
        "skill_note": "Installs the community Lean 4 skill, which teaches Lea when to use "
                      "these tools.",
    },
]


# Custom-tool entries (v2.5 E2). Same idea as a server entry: something a mathematician
# can install without knowing what a REST endpoint is.
#
# **Loogle is deliberately NOT global by default, and its copy says when not to use it.**
# It duplicates `lean_loogle` from the Lean LSP server — and T1 measured what happens when
# the agent is handed two ways to do one thing: it picks by familiarity, not by fit. The
# entry earns its place only for someone who has NOT installed the MCP server, so the
# tiebreaker is written into the tool's own "when NOT to use" text, where the model reads
# it. That is the overlap fix applied to the overlap's own cause.
TOOL_CATALOG: list[dict] = [
    {
        "id": "loogle",
        "title": "Loogle (Mathlib search by shape)",
        "summary": "Search Mathlib by type signature or name — e.g. every lemma of the "
                   "form `?a + ?b = ?b + ?a`.",
        "requires": "No API key. Public service, rate-limited to roughly 3 searches per "
                    "30 seconds.",
        "tool": {
            "name": "Loogle",
            "url": "https://loogle.lean-lang.org/json?q={query}",
            "params": ["query"],
            "authoring": {
                "summary": "Searches Mathlib for declarations matching a name, a type "
                           "signature, or a pattern with `?` holes.",
                "when_to_use": "When you know the SHAPE of the lemma you need but not its "
                               "name — e.g. `(?a + ?b) * ?c` — or you want every lemma "
                               "mentioning a constant.",
                "when_not_to_use": "Do NOT use this if the Lean LSP server is enabled: its "
                                   "`lean_loogle` does the same search and does not consume "
                                   "this shared rate limit. Do not use it for natural-"
                                   "language questions — `search_mathlib` is better at those.",
                "how": "Send a Lean type signature, a declaration name, or a pattern using "
                       "`?a`-style holes. Results are declarations with their signatures "
                       "and modules.",
            },
        },
    },
]


def tool_entries() -> list[dict]:
    return TOOL_CATALOG


def get_tool_entry(entry_id: str) -> dict | None:
    return next((e for e in TOOL_CATALOG if e["id"] == entry_id), None)


def entries() -> list[dict]:
    return CATALOG


def get(entry_id: str) -> dict | None:
    return next((e for e in CATALOG if e["id"] == entry_id), None)
