// Slash-command framework — the pure, testable core (parser + metadata registry), with
// no store/api coupling, so the composer dispatcher AND the autocomplete menu read one
// source of truth and this file unit-tests cleanly (the .js-logic + .mjs-test convention).
// The impure handlers live in slashCommandRunner.ts.
//
// Design (modelled on OpenCode / Claude Code command systems, and OpenHands' declarative
// microagents): a command is METADATA + a workflow, dispatched by the leading `/name`.
// Two kinds:
//   - 'action' — runs code and does NOT start a proof run (/compact, later /clear, /model).
//   - 'prompt' — expands a `$ARGUMENTS` template into a normal run (the extension seam for
//     future user/file-defined workflows; not wired to a handler, it seeds a run directly).
// New commands drop into SLASH_COMMANDS; nothing else changes.

/**
 * @typedef {Object} SlashCommand
 * @property {string} name                 command name, no leading slash (e.g. 'compact')
 * @property {string} description          shown in the autocomplete menu
 * @property {'action'|'prompt'} kind      'action' → handler; 'prompt' → templated run
 * @property {string} [argumentHint]       e.g. '[message]', shown as a usage hint
 * @property {string[]} [aliases]          alternate names
 * @property {string} [template]           for 'prompt' commands: body with `$ARGUMENTS`
 */

/**
 * Parse a composer input into a slash command, or null if it isn't one.
 * `/compact` → { name: 'compact', args: '' }; `/foo a b` → { name: 'foo', args: 'a b' }.
 * A bare `/` (or `/` followed by non-letters) is not a command.
 * @param {string} input
 * @returns {{ name: string, args: string } | null}
 */
export function parseSlashCommand(input) {
  const trimmed = (input || '').trim();
  if (!trimmed.startsWith('/')) return null;
  const m = trimmed.match(/^\/([a-zA-Z][\w-]*)\s*([\s\S]*)$/);
  if (!m) return null;
  return { name: m[1].toLowerCase(), args: m[2].trim() };
}

/** The command registry — metadata only. @type {SlashCommand[]} */
export const SLASH_COMMANDS = [
  {
    name: 'compact',
    description: 'Compact this conversation to free up context',
    kind: 'action',
  },
  {
    name: 'skills',
    description: 'Choose which skills this conversation uses',
    kind: 'action',
  },
  {
    name: 'mcp',
    description: 'Choose which MCP servers this conversation uses',
    kind: 'action',
    aliases: ['servers'],
  },
];

/**
 * Look up a command by name or alias.
 * @param {string} name
 * @returns {SlashCommand | undefined}
 */
export function findSlashCommand(name) {
  const n = (name || '').toLowerCase();
  return SLASH_COMMANDS.find((c) => c.name === n || (c.aliases || []).includes(n));
}

/**
 * Commands whose name starts with a partial (drops a leading slash) — for the composer
 * autocomplete. `''` or `'/'` returns all commands. `/comp` → [compact].
 * @param {string} partial
 * @returns {SlashCommand[]}
 */
export function matchSlashCommands(partial) {
  const p = (partial || '').replace(/^\//, '').toLowerCase();
  return SLASH_COMMANDS.filter((c) => c.name.startsWith(p));
}
