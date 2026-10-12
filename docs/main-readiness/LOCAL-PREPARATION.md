# Local integration preparation

The preparation remains on `LeaCheckAddition`. The reviewed VIDA main is
`8fc9a74bb7de07b655105e46fe661eb3473482b2`. Main integration, pushing, PR creation,
publication, remote CI dispatch and GitHub settings changes require later
authorization. Normal services remain stopped; tests use disposable repositories
and compiler-command fixtures without application listeners or model calls.

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
changed for this preparation; their release machinery remains intact. Their
reasons document the manual publication controls.

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
update real refs or the real index. For the current reviewed main, the 99 raw
conflicts resolve with zero candidate differences from the feature tree.

Snapshots bind exact commits, including documentation changes. An evidence
snapshot stored inside a later report commit is historical evidence, not approval
for that later HEAD. Regenerate the snapshot whenever HEAD, main, the manifest or
the source tree changes. Do not edit hashes to make an old snapshot pass.

## Pending integration, only after authorization

After reviewing a fresh preview and receiving explicit authorization to integrate
main into the feature branch, the additional `--apply` flag stages the reviewed
merge. This flag was exercised only in disposable test repositories during local
preparation; it was not used on this checkout.

The apply path rechecks the live main tip and clean branch immediately before
mutation. It disables merge hooks and automatic rerere resolution, refuses
automatic stashing and ignored-file overwrites, starts a non-fast-forward pending
merge and applies only the reviewed resolution paths. HEAD remains unchanged.
It verifies the real index tree equals the approved tree and leaves `MERGE_HEAD`
pending. It never commits, fetches, pushes, publishes or changes GitHub settings.

An unexpected post-mutation error can leave a pending merge. Inspect it and decide
whether to abort; the tool does not discard files automatically. Do not commit a
tree that failed validation. Successful staging must still receive inspection
before the separately authorized merge commit is created.

## Compiler preparation

CI cache download retries once. If both attempts fail, Lake must compile the
missing targets. Mathlib build, the no-build completeness check, SafeVerify build
and executable presence remain mandatory. Preparation honors `ELAN_HOME`, so
isolated tests can exercise the command sequence without invoking a host compiler
or installing anything.

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
the default read-only CLI.

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

The remaining stages require authorization: actual integration, feature publication
and PR review, native AMD64/ARM64 CI on the exact candidate, and enforcement. Main
merge and release are separate later decisions. The prior local production image
checks remain evidence for unchanged runtime behavior; native candidate CI will
rebuild images including these prepared scripts. Live database migration and
certificate rechecks remain deployment work.
