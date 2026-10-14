# Existing proof data review — 2026-10-07

Checked 103 canonical project/session paths against their working files using a read-only SQLite connection: 95 matched, 1 differed, and 7 were missing. This comparison includes old scratch and probe paths; a missing working file is not necessarily a lost proof, since SQL history retains its bytes.

The scan did not edit files, update database verdicts, or restore historical paths. Reconciliation requires selecting the intended revision explicitly, as specified in the implementation plan.

| Step | Path under the shared project | Finding | Current file links |
| --- | --- | --- | --- |
| 3987 | `Probe.lean` | bytes_differ | None |
| 4702 | `candidate.lean` | missing_or_unreadable | None |
| 2102 | `criticalQ.lean` | missing_or_unreadable | None |
| 1323 | `lembootstrapcentralHelpers.lean` | missing_or_unreadable | support (d35ed81a-ea7f-412f-a226-e0856d598609) |
| 1346 | `lembootstrapcentralVerification.lean` | missing_or_unreadable | support (d35ed81a-ea7f-412f-a226-e0856d598609) |
| 319 | `probe_recovery.lean` | missing_or_unreadable | generated (a75e9537-1f8c-4182-98e6-4189425580aa) |
| 194 | `probe_upper.lean` | missing_or_unreadable | generated (d8f46635-0548-4d5e-89ac-31535698f4c8) |
| 496 | `scratch_scale.lean` | missing_or_unreadable | support (d35ed81a-ea7f-412f-a226-e0856d598609) |

See [the complete path report](existing-disk-ledger-drift.json) for exact locations and identities. One aliased history path named candidate.lean is missing; the distinct nested candidate.lean used by a currently proved target exists and checked successfully.

The read model currently labels 2 of 82 formalizations proved. All 8 of their primary/support snapshots matched disk and passed real Lean recompilation. SHA-256 evidence is saved in [the recheck report](existing-proof-recheck.json). These checks did not overwrite historical verification or alter theorem statements. Recompilation confirms Lean acceptance; SafeVerify kernel replay was not rerun against this existing data.

Before rollout, review each discrepant path. Preserve the disk version of Probe.lean until its intended revision is selected. For missing linked helpers, either explicitly restore their SQL revision through the file-save API or retire obsolete links through the normal project workflow. Do not bulk-overwrite working files or silently delete history.
