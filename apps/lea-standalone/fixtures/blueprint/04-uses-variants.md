# Blueprint — Uses variants

Each node spells its `uses:` line differently.

## root
- kind: theorem
- uses: `a`, b ,c

Backticks, stray spaces and commas all separate dependencies.

## a
- kind: lemma
- uses: none

A leaf marked with the word none.

## b
- kind: lemma
- uses: —

A leaf marked with an em dash.

## c
- kind: lemma
- uses: -

A leaf marked with a hyphen.

## d
- kind: lemma
- uses: n/a

A leaf marked with n/a.

## e
- kind: lemma
- uses: a b

Two dependencies separated by whitespace only.

## f
- kind: lemma
- uses: Nothing

A leaf marked with a capitalised sentinel.
