# Blueprint — Edges

Document-level dependency problems.

## a
- kind: lemma
- uses: b

One half of a two-node cycle.

## b
- kind: lemma
- uses: a

The other half of the cycle.

## c
- kind: lemma
- uses: ghost

Depends on a key that is not a node.

## d
- kind: lemma
- uses: d

Depends on itself.
