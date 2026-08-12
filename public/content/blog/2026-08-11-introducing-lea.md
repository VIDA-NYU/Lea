---
title: "Introducing Lea: formalization that keeps the mathematician in the loop"
date: 2026-08-11
author: The Lea team
tags: [announcement, design]
description: Lea is an open-source Lean 4 agent backbone built on one premise — the mathematician steers the decomposition, intervenes mid-proof, and reviews each claim as it is established.
---

Two approaches to formalizing research mathematics have been converging on the same
answer. **Autonomous provers** optimize search over proof space, usually with
domain-specific training. **General-purpose coding agents** take an existing harness —
Claude Code, Codex, OpenHands — and wire in tools, prompts and scripts until it can
formalize a paper.

What both optimize is *autonomy*. And in both, the mathematician sits at the endpoints
of the process: supplying a target at one end, inspecting a result at the other.

That is not what mathematicians say they want. In a recent mixed-methods study of
formalization workflows, [Collins et al.](https://arxiv.org/abs/2606.04273) found that
81.7% of surveyed mathematics students and researchers preferred full or at least partial
human control over the process, and 66.7% wished to retain high-level strategic control
even while delegating the mechanical work. Six of seven user-study participants had
assembled their own multi-tool workflows, and the paper concludes that building tailored
systems is a gap.

The autonomy-first systems report the consequences themselves. Numina-Lean-Agent's case
study finds its own output overly result-oriented, and notes that the Lean it produces is
simplified by human experts before it can be maintained. LeanMarathon reports a run in
which, with the required Mathlib theory absent, its agent produced an artifact that
type-checks and passes every structural check while establishing nothing mathematically.

**Lea takes the other position.** A system for formalizing research mathematics should
place the mathematician, not the agent, at the center: you steer how the argument is
decomposed, you intervene while the proof is being built, and you review each claim as it
is established.

## A backbone, not an interface

The temptation, having said that, is to build one very good interface and stop. We think
that is the wrong shape. Mathematicians work in different places — a browser, an Overleaf
document, a terminal, an editor — and no single client is going to be right for all of
them.

So Lea is exposed as a **backbone**: the prover runs in-process behind one
application-neutral API, and applications are built against it. There is no separate
prover service to start, and no route in the API is specific to any client.

A run publishes an ordered stream of typed, meaning-level events — a turn started,
assistant text arrived, a proof file changed, a Lean check returned, a sub-agent
progressed, usage changed, the run finished. Clients render *what happened*. They never
decode prover-specific tool output, which means a new client is a weekend, not a fork.

We ship two applications on it today:

**LeaChat** is a standalone web client. You state a theorem and watch the Lean file take
shape beside the transcript, step by step, with a `lean_check` verdict attached to each
step. You can walk back through earlier versions, edit the file by hand, and hand it back.

**LeaOverleaf** is a Chrome extension plus a local companion. You mark a theorem in your
LaTeX source with a `% lea: formalize` comment and it gets formalized in place, with
`uses={…}` declaring dependencies on earlier results and `context={…}` passing strategy
hints straight through.

They share sessions, transcript and usage accounting. A formalization started in Overleaf
opens in the web client with its whole history intact, because both are reading the same
ledger.

## What the backbone actually holds

Three components form a cycle. A **formalization controller** selects the next proof
action. A **Lean project runtime** executes it in the target project. A **persistent run
ledger** records the resulting event and makes it part of the context for the next action.

The controller works on a *project* — a workspace that outlives any single run, fixing a
Lean namespace and supplying three documents on every invocation: instructions you write,
memory that both you and Lea append to, and a blueprint decomposing the target into
interdependent lemmas. Two applications continuing the same session do not merely see the
same file; they continue the same mathematical context.

None of these components is our invention, and we say so in the paper. Blueprints are
Massot's, automated since by LeanArchitect and used as a system of record by LeanMarathon.
Delegation to sub-agents is standard — OpenHands exposes it as an action. Reusable Lean
skill packs already exist. Our claim is about their *exposure*: a harness carrying these
components can be a platform that domain-specific software gets built against, rather than
a monolith each new use case has to fork.

## Two states, never collapsed

One design decision runs through every surface. Lea distinguishes **proved** from
**verified**.

Proved means the file elaborates — Lean accepted it and no `sorry` remains. Verified
additionally means it survived SafeVerify: kernel replay, per-declaration type and body
matching, and an axiom whitelist.

Those are different claims, and a system that shows one green check for both is telling you
something it does not know. The distinction is not academic: the ways to break the
relationship between a file and the statement you asked for — namespace shadowing, a
supporting definition redefined, a `sorry` arriving through an import — all leave the
compile perfectly clean.

## Where this is going

Lea is research software, in the open, and it is early. The API is single-tenant. The
evaluation is in progress. The sharp edges are real and we would rather you hit them with
us than around us.

If you are formalizing something, we would like to hear what breaks. If you want to teach
Lea your subfield's conventions, skills are markdown files and roles are YAML — no plugin
API to learn. And if you want a front end we have not built, that is precisely what the
backbone is for.

- [Install it](/install/) — Docker in one command, or a local checkout
- [Join the Discord](https://discord.gg/CtEJvUTjm) — installs, proofs, and what to build next
- [Read the source](https://github.com/VIDA-NYU/LeaUIOverleafEcosystem)
