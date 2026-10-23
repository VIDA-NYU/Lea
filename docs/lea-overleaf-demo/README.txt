LEA IN OVERLEAF -- RECORDING KIT
===============================

Start here
----------
1. In Overleaf, choose New Project > Upload Project and select
   lea-overleaf-project.zip. main.tex is the main document.
2. Open output/pdf/lea-demo-presenter-script.pdf beside the browser.
3. Keep copy-paste-snippets.txt available for the source and Lean edits.
4. Follow the script's preparation page before recording. Aim for 19 minutes;
   its final page gives a 15-minute cut.

Contents
--------
overleaf/                    Upload-ready project source, including lea-tags.sty.
presenter/                   Editable LaTeX source for the click-and-say script.
copy-paste-snippets.txt       Exact marker, prompts, source corrections, and edits.
reference/reference-proofs.lean
                             Locally checked reference proofs for rehearsal.
output/pdf/lea-demo-document.pdf
                             Preview of the four-page starting document.
output/pdf/lea-demo-presenter-script.pdf
                             Printable, timed presenter script.
lea-overleaf-project.zip     Only the files needed by the Overleaf project.
lea-demo-kit.zip             Complete kit, including PDFs and editable sources.

The examples
------------
main.tex: D(n)=n+n; D(0)=0; D(m+n)=D(m)+D(n); D(n+2)=D(n)+4.
sections/02-source-review.tex: intentionally false x^2 >= x over every real x;
                              correct it by explicitly assuming x >= 1.
sections/03-shared-results.tex: Q_a(x)=(x-a)^2, with one shared shift identity
                               used at offsets one and two.
main.tex also contains an optional standalone proposition and a Lean code block.

Initial marker count is 9; the live marker insertion makes 10. The Shift identity
is intentionally unmarked because its role is shared supporting mathematics.
The source document deliberately flags its false claim as an exercise.

Important preparation details
-----------------------------
The project already includes the extension's lea-tags.sty and package load.
The inline tags are in main.tex; the two included files use comment markers.
Upload only lea-overleaf-project.zip, not the presenter source or extra .tex
copies. Lea inventories .tex files across the project, including unused copies.

Prepare completed examples, a genuine source finding or disproof, and real shared
reuse before recording. Reference proofs are a rehearsal aid, not imported
formalization history. Keep generated imports and namespaces when editing Lean.
Do not reset shared Lea state to rehearse this kit. Use a dedicated demo project.

The script distinguishes an individual run's source-input pause from a checked
disproof and from batch behavior. It also distinguishes personal review from
Approve result on an actual review candidate. Conditional actions are described
without pretending that every run or every account will show every control.

Verification scope
------------------
See validation.txt for the preparation checks. No paid agent formalizations,
live Overleaf project changes, GitHub pushes, or imports were performed while
creating this kit. Rehearse the real extension workflow before recording.
