# Local integration preparation

The work remains on `LeaCheckAddition`. VIDA main
`8fc9a74bb7de07b655105e46fe661eb3473482b2` was integrated locally with explicit
authorization in `043f6d4`; follow-up validation setup fixes are in `3ff5bfe`.
The current preview has zero conflicts. Pushing, PR creation, publication,
remote CI dispatch and GitHub settings changes still require later authorization.
Normal services remain stopped. Validation uses isolated data and temporary
listeners; no model calls or live migration occur. See the [current report](REPORT.md).

## Publication controls

`publish-image.yml` and `site.yml` now accept only `workflow_dispatch`. Main and
tag pushes cannot start these publication workflows once this version is on main.
Application validation remains available on PRs, manual validation dispatch and
reusable workflow calls. Both release workflows retain mandatory validation.

Validation tokens have `contents: read`. Website build additionally has
`pages: read` for existing-site metadata; only deployment receives `pages: write`
and `id-token: write`. Image build and manifest publication retain their scoped
`packages: write`. These are local workflow changes; currently deployed workflows
and GitHub settings have not changed.
The metadata read permission follows the [GitHub Pages API requirement](https://docs.github.com/en/rest/pages/pages#get-a-github-pages-site).

Manual publication still requires an explicitly approved release. Restoring
automatic release triggers would be a separate reviewed change.

## Reviewed manifest and preview

The version 2 [manifest](merge-resolutions.json) preserves all 133 reviewed path
choices and additionally pins their Git modes. The baseline has 131 regular files
and two executable files. Only the two publication workflow blob selections
changed during the initial preparation; their release machinery remains intact.
Their reasons document the manual publication controls. Integration validation
subsequently updated two fixture selections for the pinned test compiler and
the asynchronous finalization wait. The 133 path choices and modes remain.

`review_state: reviewed` records recipe review, not permission to perform a merge.
`--record` produces a draft manifest; drafts cannot approve a preview. Review its
choices and commit the reviewed manifest before generating an approval snapshot.
Never use automatic recording to accept an advanced main without inspecting its
delta.

From a clean, committed `LeaCheckAddition` checkout, generate a fresh snapshot:

```bash
python3 scripts/preview-main.py --output /tmp/lea-main-approved-preview.json
python3 scripts/apply-main-resolutions.py --approved-preview /tmp/lea-main-approved-preview.json
```

The second command defaults to read-only preflight. It verifies the actual branch,
feature commit, local main commit, live remote main, manifest SHA-256, source tree,
resolved tree and selected paths. It rejects dirty staged, unstaged or untracked
work, existing Git operations, unsupported legacy evidence and changed baselines.
Ignored local proof/data files remain outside the source snapshot. An explicit
collision check rejects incoming paths that would replace an ignored file,
directory or symlink before Git starts a merge.

The preview uses a temporary index and object directory, supports worktree Git
directories, and preserves executable modes, symlinks and intentional deletions.
It includes tracked deletions in its source snapshot. Its synthetic commits never
update real refs or the real index. Before integration, 99 raw conflicts resolved
with zero candidate differences from the feature tree. Main is now an ancestor
of the feature and the preview reports zero conflicts.

Snapshots bind exact commits, including documentation changes. An evidence
snapshot stored inside a later report commit is historical evidence, not approval
for that later HEAD. Regenerate the snapshot whenever HEAD, main, the manifest or
the source tree changes. Do not edit hashes to make an old snapshot pass.

## Future main integration, only after authorization

After reviewing a fresh preview and receiving explicit authorization to integrate
main into the feature branch, the additional `--apply` flag stages the reviewed
merge. The initial preparation exercised this flag only in disposable test
repositories. The authorized integration then used it on this checkout, inspected
the pending tree and created local merge commit `043f6d4`. Apply refuses to merge
an already-integrated main a second time. A new main tip requires reviewing its
delta and refreshing the manifest, not changing hashes to bypass preflight.

The apply path rechecks the live main tip and clean branch immediately before
mutation. It disables merge hooks and automatic rerere resolution, refuses
automatic stashing and ignored-file overwrites, starts a non-fast-forward pending
merge and applies only the reviewed resolution paths. HEAD remains unchanged.
It verifies the real index tree equals the approved tree and leaves `MERGE_HEAD`
pending. It never commits, fetches, pushes, publishes or changes GitHub settings.

An unexpected post-mutation error can leave a pending merge. Inspect it and decide
whether to abort; the tool does not discard files automatically. Do not commit a
tree that failed validation. Successful staging must still receive inspection
before committing. An explicit request to integrate main locally authorizes that
inspection and local merge commit; it does not authorize pushing or merging the
feature into main.

## Compiler preparation

CI cache download retries once. If both attempts fail, Lake must compile the
missing targets. Mathlib build, the no-build completeness check, SafeVerify build
and executable presence remain mandatory. Preparation honors `ELAN_HOME`, so
isolated tests can exercise the command sequence without invoking a host compiler
or installing anything. It selects the repository `lean-toolchain`, overriding an
incorrect inherited compiler selection, and writes it to `GITHUB_ENV` in CI.
Both the application and real-Lean jobs provision these compiler prerequisites.
Companion fixtures copy that same toolchain rather than selecting floating stable.

Run the local tests and syntax checks:

```bash
python3 -m unittest discover -s tests/local_preparation -v
bash -n scripts/prepare-ci-lean.sh
```

Application CI includes the preparation suite. Actionlint additionally validates
all three workflows. The fixture tests cover successful downloads, a recovered
transient failure, exhausted downloads followed by successful compilation, and
fatal build/completeness/verifier failures. Git tests exercise preview and actual
pending-merge behavior inside disposable repositories, plus rejection cases and
the default read-only CLI. All 38 preparation tests pass, including toolchain
selection and export for later CI steps.

For application or compiler tests that include standalone Lean inputs, pin the
test process to the installed repository version:

```bash
ELAN_TOOLCHAIN="$(cat apps/lea-standalone/prover/workspace/lean-toolchain)" npm test
cd apps/lea-standalone/adapter
ELAN_TOOLCHAIN="$(cat ../prover/workspace/lean-toolchain)" .venv/bin/python -m pytest -W error::pytest.PytestUnhandledThreadExceptionWarning
```

This changes the test process environment only. The post-fix root JavaScript
suite also passes without a global override because its temporary projects now
copy the pinned version.

## Proposed main enforcement, not applied

Once authorized, a repository administrator should apply and verify:

| Setting | Proposed value |
| --- | --- |
| Target | VIDA `main` |
| Pull request | Required |
| Approving reviews | At least one |
| Stale approvals | Dismiss after substantive updates |
| Required check | Actual emitted GitHub Actions `merge-gate` context |
| Candidate against main | Require up-to-date status checks |

Observe the exact check context and GitHub Actions application on an authorized
PR before configuring enforcement. Current main was verified without branch
protection or effective rules during planning. This preparation contains no API
mutation or enforcement bypass.

Current main integration is complete. The remaining stages require authorization:
feature publication and PR review, native AMD64/ARM64 CI on the exact candidate,
and enforcement. Main
merge and release are separate later decisions. The prior local production image
checks remain evidence for unchanged runtime behavior; native candidate CI will
rebuild images including these prepared scripts. Live database migration and
certificate rechecks remain deployment work.
