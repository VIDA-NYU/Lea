# Blueprint — Demo

A three-node plan: a helper lemma, the main theorem that uses it, and a definition not yet in Lean.

## helper
- kind: lemma
- lean: `Lea.Demo.helper`
- file: `Helper.lean`

A proved helper.

## main
- kind: theorem
- lean: `Lea.Demo.main`
- file: `Main.lean`
- uses: helper

The main theorem, using the helper.

## todo
- kind: definition

A planned definition with no Lean yet.
