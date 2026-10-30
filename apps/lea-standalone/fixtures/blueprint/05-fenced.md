# Blueprint — Fences

The preamble quotes an example node inside a fence; it must stay preamble prose:

```markdown
## fake
- kind: lemma
```

## real
- kind: lemma

```lean
theorem real : 1 + 1 = 2 := by
  norm_num
```

A fence inside a statement is inert as well:

```text
## also_fake
- uses: real
```
