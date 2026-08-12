import type { AuthoringFieldValues } from '../lib/api';

// The guided authoring form (v2.5 C1/C3) — one component, three consumers: skills,
// sub-agent roles, and later custom tools.
//
// "Write a skill" produces a blank page. Four questions produce the four things a good
// description actually contains — and the third is the one nobody writes unprompted,
// which is why an agent with overlapping tools reaches for the wrong one.
//
// The answers compile server-side into the text the model already reads (a skill's body,
// a role's instructions), so nothing downstream learns a new shape. For a role,
// "when to use this" also becomes the line the coordinator reads while choosing which
// sub-agent to delegate to — writing it well IS what makes delegation work.

export const EMPTY_AUTHORING: AuthoringFieldValues = {
  summary: '',
  when_to_use: '',
  when_not_to_use: '',
  how: '',
};

type FieldSpec = {
  key: keyof AuthoringFieldValues;
  label: string;
  hint: string;
  rows: number;
  placeholder: string;
};

// Copy is deliberately concrete and non-technical: a mathematician should be able to
// answer each of these about their own practice without knowing what a prompt is.
function specs(kind: 'skill' | 'role' | 'tool'): FieldSpec[] {
  const thing = kind === 'skill' ? 'this' : kind === 'role' ? 'this assistant' : 'this tool';
  return [
    {
      key: 'summary',
      label: 'What is it?',
      hint: 'One or two sentences, as you would explain it to a colleague.',
      rows: 2,
      placeholder:
        kind === 'role'
          ? 'A helper that looks for counterexamples before committing to a proof.'
          : 'Tactics for goals about commutative rings.',
    },
    {
      key: 'when_to_use',
      label: `When should Lea use ${thing}?`,
      hint:
        kind === 'role'
          ? 'Lea reads this when deciding which assistant to hand work to, so be specific about the situation.'
          : 'Describe the situation, not the steps.',
      rows: 3,
      placeholder:
        kind === 'role'
          ? 'When a statement looks false, or a proof attempt has failed the same way twice.'
          : 'When the goal is a polynomial identity over a commutative ring.',
    },
    {
      key: 'when_not_to_use',
      label: `When should Lea NOT use ${thing}?`,
      hint: 'The most useful box here. Without it Lea will reach for this in cases it does not fit.',
      rows: 3,
      placeholder:
        kind === 'role'
          ? 'When the statement is routine, or already close to compiling.'
          : 'For inequalities, or anything involving division.',
    },
    {
      key: 'how',
      label: 'How is it done?',
      hint: 'Your actual method — tactics you reach for, the order you try them, what usually goes wrong.',
      rows: 6,
      placeholder:
        kind === 'role'
          ? 'Look for a small case that breaks it. Try n = 0 and n = 1 first…'
          : 'Try `ring` first. If that fails, `ring_nf` then `linarith`…',
    },
  ];
}

export function AuthoringFields({
  kind,
  value,
  onChange,
  disabled,
}: {
  kind: 'skill' | 'role' | 'tool';
  value: AuthoringFieldValues;
  onChange: (next: AuthoringFieldValues) => void;
  disabled?: boolean;
}) {
  return (
    <div className="authoring">
      {specs(kind).map((f) => (
        <label className="mcp-field" key={f.key}>
          <span className="mcp-label">{f.label}</span>
          <textarea
            className="sf-textarea mcp-mini"
            rows={f.rows}
            value={value[f.key] ?? ''}
            placeholder={f.placeholder}
            onChange={(e) => onChange({ ...value, [f.key]: e.target.value })}
            disabled={disabled}
            spellCheck
          />
          <span className="mcp-hint">{f.hint}</span>
        </label>
      ))}
    </div>
  );
}

/** True when the author has actually answered something — used to decide whether the
 *  guided fields or the raw text is the source of truth for a save. */
export function hasAuthoring(value: AuthoringFieldValues | undefined | null): boolean {
  return Object.values(value || {}).some((v) => (v || '').trim().length > 0);
}
