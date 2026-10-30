import { create } from 'zustand';

/**
 * The files package's own store (docs/PLAN-ui-redesign.md §3 "files").
 *
 * Three hand-offs to the composer and the canvas that the files components
 * cannot reach directly; the integrator wires them into ChatThread / Canvas:
 *
 *   composerContext      "Ask Lea about this file", "Prove the sorries here"
 *                        (files rail menu) and ⌘⏎ in the ⌘P palette set it.
 *                        ChatThread shows it as the chip above the composer
 *                        ("Parity.lean · in context") and clears it on ×; App's
 *                        handleSubmit prepends composerContextHeader() to the
 *                        next message and clears it once the run is admitted.
 *   pendingComposerText  `> command` in the palette: the text to put in the
 *                        composer draft (e.g. "/compact "). ChatThread reads it,
 *                        calls setDraft, focuses the composer, then clears it.
 *   pendingLine          `: line` in the palette and a declaration hit: the
 *                        1-based line the canvas should reveal in the file just
 *                        opened through useWorkbench().openFile. The canvas
 *                        scrolls to it and clears it.
 */

export type ComposerIntent = 'ask' | 'prove-sorries';

export interface ComposerContext {
  /** Repo-relative path of the file in context. */
  path: string;
  intent: ComposerIntent;
  /** 1-based line, when the context is a declaration or a `: line` hit. */
  line?: number;
  /** Short chip text — a declaration name, for instance. Defaults to the basename. */
  label?: string;
}

interface FilesState {
  composerContext: ComposerContext | null;
  setComposerContext: (context: ComposerContext | null) => void;
  pendingComposerText: string | null;
  setPendingComposerText: (text: string | null) => void;
  pendingLine: number | null;
  setPendingLine: (line: number | null) => void;
}

export const useFiles = create<FilesState>((set) => ({
  composerContext: null,
  setComposerContext: (composerContext) => set({ composerContext }),
  pendingComposerText: null,
  setPendingComposerText: (pendingComposerText) => set({ pendingComposerText }),
  pendingLine: null,
  setPendingLine: (pendingLine) => set({ pendingLine }),
}));

/**
 * What the composer should show for a context: the chip text, a placeholder
 * and, for "prove the sorries", a message the integrator may prefill.
 */
export function describeComposerContext(context: ComposerContext): {
  chip: string;
  placeholder: string;
  prefill: string | null;
} {
  const name = context.label ?? context.path.slice(context.path.lastIndexOf('/') + 1);
  if (context.intent === 'prove-sorries') {
    return {
      chip: name,
      placeholder: `Prove the sorries in ${name}…`,
      prefill: `Prove the remaining sorries in ${context.path}.`,
    };
  }
  return {
    chip: context.line ? `${name}:${context.line}` : name,
    placeholder: `Ask about ${name}, or let Lea finish…`,
    prefill: null,
  };
}

/**
 * The one-line header App prepends to a message sent while a file is in
 * context, so Lea sees which file (and line) the message is about and what
 * the user wants done with it.
 * TODO(backend): send the context as a field on the run request (`file:
 * { path, line?, intent }`) instead of a text header, so the adapter can attach
 * the file's current contents and the transcript shows the message as typed.
 */
export function composerContextHeader(context: ComposerContext): string {
  const where = context.line ? `${context.path}:${context.line}` : context.path;
  const name = context.label ? ` (${context.label})` : '';
  const intent = context.intent === 'prove-sorries' ? 'prove the sorries here' : 'ask about this file';
  return `[File in context: ${where}${name} · ${intent}]`;
}
