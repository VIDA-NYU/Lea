# Plan — One-Click Install: UI + Overleaf Extension for Non-Technical Users

Goal: someone with no terminal experience gets both front ends running in the
fewest steps physically possible. The target end-state user flow is:

1. **Install Docker Desktop** (one-time, guided link).
2. **Download and double-click the Lea launcher** (one file from the releases
   page; it pulls the prebuilt image, starts everything, opens the browser).
3. **Paste an API key** into the Settings pane.
4. *(Overleaf users only)* **Click "Add to Chrome"** on the Web Store listing.

That is 3 steps for the standalone UI, 4 including Overleaf — no git clone, no
`npm`, no `uv`, no `elan`, no `.env` editing, no `chrome://extensions`
developer mode, no terminals left open.

## 1. Grounding — what's there today, confirmed by reading the code

1. **A self-contained Docker image already exists but is never published.**
   `apps/lea-standalone/Dockerfile` bakes frontend + adapter + in-process
   prover + Lean + Mathlib into one arch-independent image, and even documents
   the `docker buildx --platform linux/amd64,linux/arm64 … --push` command in
   a comment — but there is no CI workflow (`.github/workflows/` doesn't
   exist) and `docker-compose.yml` builds locally (`image: leaui:local`).
   Every user today pays the full multi-gigabyte Mathlib bake on their own
   machine, and needs the repo cloned to do it.
2. **The macOS launcher already assumes the missing piece.**
   `apps/lea-standalone/start-lea.command` says "Pull the prebuilt image from
   Docker Hub" and waits on `/api/health` before opening the browser — but
   compose actually builds from source, so the launcher's promise is wrong
   today. The launcher shape (check Docker → start → open browser) is right;
   it just needs a published image and to be distributable without the repo.
3. **The Docker path explicitly excludes Overleaf.** README: "Docker covers
   the standalone UI only. The Overleaf extension needs the local install
   below." So the *harder* audience (Overleaf users) is stuck with the
   *harder* path: `install.sh` (installs uv + elan), `npm run setup`
   (Mathlib download), two long-running terminals (`start:adapter`,
   `dev:overleaf`), then side-loading an unpacked extension in developer mode
   and checking two options-page settings (`BETA_INSTALL.md`, 7 sections).
4. **The companion cannot run standalone on a bare machine — but is a perfect
   fit for the existing container.** `companion/server.mjs:4811` spawns
   `lake env lean <path>` directly, and `companion/leanDependencyGraph.mjs`
   reads proof files straight from `leaRepoPath` on disk. So the companion
   needs the Lean toolchain *and* shared filesystem with the prover workspace
   — exactly what the image already contains (`/app/prover`, elan/lake on
   PATH, the adapter venv at `/app/adapter/.venv` which matches
   `companion/doctor.mjs`'s relative expectations). It only additionally
   needs Node, which the image already uses in its build stage.
5. **Key entry is already terminal-free.** The Settings pane writes the
   provider key to `config/lea.local.toml` (mounted volume in compose); no
   `.env` editing is required for the Docker path. The companion itself holds
   no provider key — runs go through the adapter.
6. **The extension already defaults to the right companion URL.**
   `applyEnvDefaults` defaults `leaApiBaseUrl` to `http://127.0.0.1:8001` and
   the extension defaults its companion URL to `http://127.0.0.1:31245`;
   `BETA_INSTALL.md` steps 6.7–6.8 only *confirm* defaults. The extension is
   an ordinary MV3 extension (`manifest.json`, version 0.1.0, no `key`/
   `update_url`) that has simply never been packaged or published.

Conclusion: nothing structural blocks the target flow. All four phases below
are packaging and distribution work, not architecture work.

## 2. Phase 1 — Publish the prebuilt image (kills clone + build + toolchains)

The single highest-leverage change: turn "clone the repo and bake Mathlib
locally" into "docker pulls a finished image."

1. **CI workflow** (`.github/workflows/publish-image.yml`): on release tag
   (and manual dispatch), `docker buildx build --platform
   linux/amd64,linux/arm64` and push to GHCR
   (`ghcr.io/darturi/leaui:<tag>` + `:latest`). GHCR avoids Docker Hub
   rate-limit and account friction; the Dockerfile comment already prescribes
   the exact command. Build on the release runner with layer caching; the
   Mathlib layer only rebuilds when `prover/` changes.
2. **Split compose into user vs. dev.** User-facing
   `docker-compose.yml`: `image: ghcr.io/darturi/leaui:latest`, no `build:`
   — `docker compose up` pulls. Dev keeps the local build via
   `docker compose -f docker-compose.dev.yml` (or a `build` profile). This
   also makes `start-lea.command`'s existing "pull the prebuilt image" text
   true.
3. **Image size pass (best-effort, not blocking).** The image will be several
   GB regardless (Mathlib oleans). Already trimmed: package `.git` dirs,
   mathlib cache. Worth checking: elan toolchain doc/test payloads, adapter
   build artifacts. Publish with zstd compression to cut pull time. The
   launcher (Phase 4) sets expectations: "first start downloads ~X GB."

Exit criteria: on a machine with only Docker installed and **no repo clone**,
`docker run -p 8001:8001 -v …data -v …config ghcr.io/darturi/leaui:latest`
serves the working UI at `localhost:8001`.

## 3. Phase 2 — One container runs the whole backend (brings Overleaf into the easy path)

Add the companion to the same image so `docker compose up` is the entire
backend for *both* front ends. Per grounding §1.4, everything the companion
shells out to and reads is already in the image.

1. **Bundle the companion.** In the Dockerfile runtime stage: install Node 22
   (or copy the static `node` binary from the existing `node:22-bookworm-slim`
   build stage), then `COPY` `apps/overleaf-extension/{companion,shared}`,
   `packages/lea-model-catalog`, and `scripts/env.mjs` preserving their
   relative layout (the companion imports both via `../../../` relative
   paths — replicate the monorepo shape under `/app`, adjusting the prover
   path only through config, which `LEA_ROOT` already controls).
2. **Supervise both processes.** Extend `docker/entrypoint.sh` to launch the
   adapter and the companion, forward signals, and exit if either dies.
   In-container env: `LEA_ROOT=/app/prover`,
   `LEA_API_BASE_URL=http://127.0.0.1:8001` (same container),
   `LEA_UI_BASE_URL=http://localhost:8001` (in Docker the UI is the SPA
   served by the adapter, not Vite on :5173 — this is what the extension's
   "open in Lea UI" links use).
3. **Expose and persist.** Map `31245:31245` alongside `8001:8001`; volume
   for the companion's state dir (`.overleaf-lean-stub/` job logs + settings)
   next to the existing `data`/`config` mounts.
4. **Health.** Extend the container healthcheck (or add a compose-level one)
   to also probe the companion's health endpoint, so the launcher's "ready"
   signal means both front ends work.

Exit criteria: with only the published image, the Chrome extension (loaded
any way) formalizes theorems end-to-end against `127.0.0.1:31245` — zero
local toolchain.

## 4. Phase 3 — Chrome Web Store listing (kills developer-mode side-loading)

1. **Package pipeline.** CI job that zips `extension/` with a version bump
   check (manifest `version` must match the release tag). Audit for CWS
   review compliance first: MV3 remote-code rules (the lazily-imported
   `.mjs` modules are fine — they're in-package; `pageBridge.js` injection
   into the Overleaf page needs to be via `chrome.scripting`/web-accessible
   resource, not remote fetch), narrowest host permissions
   (`https://www.overleaf.com/*` plus `http://127.0.0.1:31245/*` — localhost
   permissions are allowed and should be declared explicitly), and a privacy
   disclosure ("sends marked theorem text to a server on your own machine").
2. **Publish unlisted first.** Register the CWS developer account ($5,
   one-time) **now** — first-submission review is the longest lead-time item
   in this whole plan, so start it before Phases 1–2 finish. Unlisted gets
   beta testers "Add to Chrome" via link; flip to public when ready.
3. **Zero-config on install.** Verify the fresh-install defaults reach the
   companion with no options-page visit (grounding §1.6 says they should).
   The "Lea repo path" option becomes irrelevant in Docker mode (the path is
   inside the container) — hide it behind an "advanced" section or drop it
   from the options UI for the packaged build.
4. **Fallback artifact.** Keep attaching the zip to GitHub Releases so
   power users / reviewers can still side-load a pinned version;
   `BETA_INSTALL.md`'s load-unpacked section shrinks to that fallback.

Exit criteria: a beta tester with the container running installs the
extension by clicking one link, opens Overleaf, and it works with zero
configuration.

## 5. Phase 4 — Single-file launchers + a real download page (kills the clone)

1. **Make the launcher self-contained.** Today `start-lea.command` needs the
   repo (it cds to its own dir and runs `docker compose`). Rewrite it to be
   downloadable as one file: embed the tiny compose file (heredoc into
   `~/Lea/`) or use a plain `docker run` with the port maps and
   `~/Lea/{data,config,overleaf-state}` volume mounts, then the existing
   check-Docker → wait-for-health → open-browser logic. Keep data under
   `~/Lea` so it survives and is findable.
2. **Windows and Linux variants.** `Lea.bat`/`Lea.ps1` (Docker Desktop on
   Windows implies WSL2 — the launcher should detect Docker missing/not
   running and show the same friendly guidance as the macOS one) and
   `lea.sh`. Same behavior in all three: idempotent, re-runnable, Ctrl+C or
   window-close stops the stack.
3. **Release page as the landing page.** Each GitHub Release carries: the
   three launchers, the extension zip, and a short body with exactly the
   4-step flow from the top of this plan (Docker Desktop link, launcher
   download, key link, Add-to-Chrome link). Rewrite the README quick start
   to lead with this and demote the toolchain install to a
   "Developing" / CONTRIBUTING section. `BETA_INSTALL.md` gets replaced by
   the 4-step flow plus troubleshooting.
4. **Explicitly deferred: a native tray app.** A Tauri/Electron "Lea
   Desktop" that manages the container would polish step 2 further, but it
   adds signing/notarization and an update channel for marginal gain over a
   double-clickable script. Revisit only if the launcher scripts prove
   confusing in beta.

Exit criteria: a fresh machine with Docker Desktop goes from the release
page to a working UI + working Overleaf extension without ever opening a
terminal or cloning the repo.

## 6. Phase 5 — First-run experience (makes step 3 self-explanatory)

1. **First-run wizard in the UI.** When no provider key is configured, land
   on a guided Settings state: pick provider → deep-link to that provider's
   key page (the three links already in the README) → paste → live
   validation call → "you're ready." The Settings pane already persists to
   `lea.local.toml`; this is presentation work.
2. **Connectivity self-diagnosis in the extension.** The popup/pane should
   distinguish "companion unreachable" (→ "Is Lea running? Double-click the
   Lea launcher") from "companion up, adapter down" and "no API key yet"
   (→ "Open Lea Settings" link to `localhost:8001`). Surface
   `doctor`-equivalent checks as human sentences, not checkmarked CLI
   output.
3. **In-app update nudge (cheap version).** The container knows its image
   tag; the UI can compare against the latest GitHub release and show
   "update available — restart the launcher" rather than requiring any
   update tooling.

## 7. Sequencing and effort

| Phase | Size | Depends on | Start when |
|---|---|---|---|
| 1 image publish | S — CI + compose split | — | now |
| 3.2 CWS account + first submission | S but **long review latency** | zip of current extension | now, in parallel |
| 2 companion-in-container | M — Dockerfile + entrypoint + volume | 1 | after 1 |
| 4 launchers + release page | S | 1 (UI-only value), 2 (full value) | alongside 2 |
| 3 rest (zero-config, pipeline) | S–M | 2 | after 2 |
| 5 first-run polish | M | none technically | anytime; before public beta |

Phases 1 + 4 alone already deliver the 3-step standalone-UI flow. Phase 2 is
the unlock that moves Overleaf users off the toolchain path entirely; Phase 3
removes the last scary step (developer mode); Phase 5 is polish that makes
the remaining steps self-guiding.

## 8. Risks and open questions

- **Image size / first-pull time.** Several GB is inherent (Mathlib). Set
  expectations in the launcher; zstd-compressed GHCR layers help. If it ever
  becomes the top complaint, the escape hatch is downloading the Mathlib
  cache at first *run* into a volume instead of baking it into the image —
  a significant Dockerfile restructure, so not in scope now.
- **Docker Desktop is itself the biggest remaining ask.** For a truly
  zero-install experience the only real alternative is a hosted backend,
  which flips the trust and cost model (user keys and Lean compute on a
  server) — out of scope, but worth naming as the eventual "step 1 killer."
  Note Docker Desktop's license: free for individuals/small orgs; fine for
  the beta audience.
- **CWS review outcomes are not fully predictable** (host permissions on
  overleaf.com + localhost connect permissions + "communicates with local
  software" disclosures). Mitigation: submit early (unlisted), keep the
  side-load zip as the fallback path.
- **CPU-arch coverage.** buildx covers mac (arm64/amd64), Windows/Linux
  amd64. Windows-on-ARM Docker runs amd64 images under emulation — slow but
  functional; document rather than solve.
- **Companion path assumptions in-container.** `doctor.mjs` derives the
  adapter venv from `leaRepoPath/../adapter` — the image's `/app` layout
  happens to match, but Phase 2 should add a container smoke test asserting
  companion doctor passes inside the image, so future layout drift is caught
  in CI.
- **Keeping dev ergonomics.** The dev compose (local build) and the existing
  `install.sh` toolchain path must keep working for development — the vendored
  prover's editable-install live-reload only exists in the local path.
