import { EditorClient } from './editorClient.mjs';
export interface Position { line: number; character: number }
export interface PlainGoal { rendered: string; goals: string[] }
export interface TermGoal { rendered: string }
type CursorEditor = { getPosition(): { lineNumber: number; column: number } | null };
/** Typed queries always publish the exact local buffer before queue admission. */
export class LeanQuery {
  constructor(private client: EditorClient, private editor: CursorEditor) {}
  ready(): boolean { return Boolean(this.client.owner) && !this.client.disposed; }
  cursor(): Position | null {
    const p = this.editor.getPosition();
    return p ? { line: p.lineNumber - 1, character: p.column - 1 } : null;
  }
  async plainGoal(pos = this.cursor()): Promise<PlainGoal | null> { return pos ? this.client.request('editor.goals', pos) : null; }
  async termGoal(pos = this.cursor()): Promise<TermGoal | null> { return pos ? this.client.request('editor.term_goal', pos) : null; }
}
