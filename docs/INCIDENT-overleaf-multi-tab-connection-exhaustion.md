# Incident: Overleaf multi-tab connection exhaustion

> Confirmed 2026-08-04. This document records the cause of the Lean pane
> becoming stuck on **Loading project inventory** and reporting that the
> project namespace is unavailable when several Overleaf tabs are open.

## Summary

Every Overleaf tab containing the Lea extension opens its own long-lived
Server-Sent Events (SSE) connection to the local companion server. The
companion serves those streams and all ordinary extension requests from the
same HTTP/1.1 origin, `http://127.0.0.1:31245`.

With enough Overleaf tabs open, the persistent streams can consume the
browser's available HTTP/1.1 connections to that origin. Requests needed to
load the project identity, namespace, theorem statuses, and Lean pane manifest
then wait for a connection rather than reaching the companion. Closing one
Overleaf tab closes its event stream, releases a connection, and allows the
queued requests to proceed.

This is a **browser-to-companion connection-capacity problem**, not a cap on
formalization jobs, adapter runs, or prover capacity.

## User-visible symptoms

The failure can affect both new and existing Overleaf projects and commonly
appears as:

- The Lean pane remains on **Loading project inventory** indefinitely.
- The extension reports **Namespace unavailable**.
- Refreshing the affected Overleaf tab does not reliably help.
- The companion and adapter may both remain healthy when checked directly.
- Closing another Overleaf tab causes the affected tab to begin working
  immediately.

The cross-project behavior is expected: all Overleaf tabs share the same local
companion origin and therefore compete for the same browser connection pool.

## Relevant architecture

The extension's push channel was introduced to make updates immediate while
retaining polling as a slower reconciliation fallback:

```text
Overleaf tab A -- EventSource /events?projectId=A --\
Overleaf tab B -- EventSource /events?projectId=B ---+--> companion :31245
Overleaf tab C -- EventSource /events?projectId=C --/        (HTTP/1.1)

Overleaf tabs -- /project/identity, /statuses,
                 /lean-pane/manifest, ... -----------> same origin
```

The relevant implementation points are:

- [`extension/content.js`](../apps/overleaf-extension/extension/content.js)
  starts the events client when each content script initializes. A content
  script is instantiated separately in every matching Overleaf tab.
- [`extension/eventsClient.mjs`](../apps/overleaf-extension/extension/eventsClient.mjs)
  creates one `EventSource` for the companion's `/events` endpoint and
  reconnects it after failures.
- [`companion/server.mjs`](../apps/overleaf-extension/companion/server.mjs)
  uses `node:http` and keeps every `/events` response open with
  `Content-Type: text/event-stream`, `Connection: keep-alive`, and periodic
  keepalive comments.

The `projectId` query parameter filters which project events a stream receives;
it does not combine or deduplicate connections across tabs.

## Evidence

During the incident, local socket inspection showed exactly six established
Chrome connections to `127.0.0.1:31245`, in addition to the companion's
listening socket. Closing one Overleaf tab immediately restored the inventory
and namespace requests.

That observation is consistent with the browser's per-origin HTTP/1.1
connection pool being occupied by six persistent SSE requests. The number six
is the observed threshold in this environment, not an application constant or
a portable guarantee; a browser, version, proxy, or network configuration may
have a different effective limit.

A useful diagnostic command is:

```bash
lsof -nP -iTCP:31245
```

Browser developer tools can provide a second signal: `/events` requests remain
open as expected, while inventory or identity requests remain queued or
pending without receiving a response.

## Why the displayed errors are misleading

The Lean pane derives its namespace and inventory from ordinary companion HTTP
requests. When those requests cannot acquire a connection, the UI has no
response from which to distinguish connection starvation from a genuinely
missing namespace. It therefore remains in its loading state or presents its
generic namespace-unavailable fallback.

The namespace itself is not necessarily missing, and restarting the prover is
not a targeted remedy. Restarting the companion may also provide only temporary
relief because all open tabs can immediately reconnect their event streams and
recreate the same condition.

## Immediate workaround

Until the connection lifecycle is changed:

1. Close unused Overleaf tabs, especially tabs with the Lea extension active.
2. Reload the affected tab after a connection has been released if it does not
   recover on its own.
3. Do not treat the symptom as evidence that formalization jobs must be
   cancelled; the exhausted resource is the browser's companion connection
   pool.

## Recommended remediation

### Immediate fix: visible-tab event streams

Only a visible Overleaf tab should hold an SSE connection:

- Start or reconnect the events client when `document.visibilityState` becomes
  `visible`.
- Stop and close it when the document becomes hidden.
- Perform an immediate inventory/status reconciliation when a tab becomes
  visible so events missed while hidden are recovered.
- Retain the existing polling path as the correctness fallback. Hidden tabs may
  use a slow polling cadence if background freshness is required.

This is the smallest change that removes persistent connections from ordinary
background tabs. Multiple simultaneously visible Overleaf windows can still
open multiple streams, but they no longer scale with every open tab.

### Stronger fix: one extension-wide event broker

For a hard one-connection invariant, an extension-owned background component
can maintain a single companion event stream and distribute project-scoped
notifications to content scripts through extension messaging. In Manifest V3,
the design must account for service-worker suspension; a fetch stream, offscreen
document, or reconnect-and-reconcile strategy should be chosen deliberately.

This is architecturally stronger but more involved than visibility-based
lifecycle management.

### Alternative: HTTP/2

Serving the companion over HTTP/2 would allow requests to share a multiplexed
connection, but it adds local protocol and certificate complexity and does not
correct the extension's unnecessarily per-tab push architecture. It is not the
preferred first fix.

## Acceptance criteria for the fix

- Opening more than six Overleaf tabs does not prevent any tab from loading its
  project identity, namespace, statuses, or Lean pane manifest.
- Hidden tabs release their event connections within a bounded interval.
- Returning to a hidden tab reconnects push updates and immediately reconciles
  state missed while it was hidden.
- Repeated visibility changes do not create duplicate event streams, timers, or
  event handlers.
- Push failure continues to fall back to polling without leaving the pane in a
  permanent loading state.
- Automated tests cover idempotent event-client start/stop behavior and the
  visibility lifecycle; a browser-level regression check covers many open tabs.

## Scope distinction

The adapter has its own queue for formalization runs, and the Lean pane has
separate queued/in-progress UI states. Those mechanisms control proof work.
They are independent of this incident, which occurs before affected HTTP
requests reach the companion and can happen even when no formalization is
running.
