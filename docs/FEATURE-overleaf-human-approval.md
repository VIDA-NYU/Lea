# Human Approval Notes

The Overleaf extension shows a separate checkmark for a user to mark a proof or
definition as personally audited. This is deliberately a UI-only note:

- it does not change Lea prompts, runs, checking, or formalization status;
- it is not written into the LaTeX or Lean source;
- it is stored in the browser profile with `chrome.storage.local`.

An approval is bound to an opaque revision computed by the companion from:

1. the current formalization input (statement, target kind, `uses`, and
   `context`);
2. the exact current Lean artifact;
3. every transitively imported project-local Lean file.

The extension removes the stored note when a later status or Lean-pane manifest
reports that the item is stale/ineligible or has a different revision. Removing
the note, rather than merely hiding it, prevents an approval from reappearing if
the source is later changed back.

Only current, checked, sorry-free proofs and definitions with no sorry-stubbed
project dependencies are eligible. Counterexamples, stubs, failed items, and
in-progress runs remain unapproved.

The checkmark appears beside both the in-source Lea status badge and the matching
Lean-pane item. Both controls read and write the same browser-local record,
keyed by Overleaf project, target kind, and marker label. Chrome storage change
events keep multiple open Overleaf tabs synchronized.
