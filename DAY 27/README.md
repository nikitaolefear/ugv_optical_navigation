# DAY 27 — UGV Stage 3 checkpoint, 2026-11-27

- Original work HEAD: `ab31cad0a248bd0346edc32726a6d83f75efa811`.
- Public archive branch parent: `69e4eec71db3a28427c5c1b738226d2e14569c47`.
- Offline tests: **90/90 PASS**. Four operator tests also passed separately against the minimal B1/B2 IPC overlay.
- **ROOT CAUSE: INCONCLUSIVE.**
- **SOFTWARE READY FOR BENCH DEPLOYMENT: YES — B1/B2 only.**
- **HARDWARE BENCH VALIDATED: NO.**
- **READY FOR STAGE 3 LIVE PREFLIGHT: NO.**
- **STAGE 3 NAVIGATION VALIDATED: NO.**

## Inventory

`01` contains the offline A/B/C runner and evidence checks. `02` contains the owner adapter and lease prototype. `03` through `05` contain successive integration, crash-safety, and transport candidates, with diffs, test results, and manifests. `06` contains the latest full candidate and distinct minimal B1/B2 patch, the read-only Lua observer, operator client, and hardware bench plan. `INVENTORY.tsv` maps input names to stored files, including deduplicated content and redactions. `SHA256SUMS` covers this public artifact set. Nested manifests document their original packages and may also refer to private files.

## Findings

The observed bench test had a 168.786 s transmit window at approximately 8.77 Hz. The estimator reported constant-position mode and no valid horizontal position. The available historical DataFlash files did not resolve SRC activation, reception, or fusion in the actual test window. The repeat package adds evidence collection and a staged read-only deployment path.

## Restore

1. Check out this backup branch in an isolated directory and run `sha256sum -c SHA256SUMS` from `DAY 27`.
2. To reconstruct the exact host-specific operator package, obtain the protected DAY 27 local archives from the repository owner and verify their SHA256 sums. Extract them into a private staging directory. Compare the operator package against its original manifest. The private Git bundle restores the original work HEAD on top of the public parent.
3. Review the original operator instructions and verify both diffs with `git apply --check` against their documented distinct baselines. The public branch does not include the unpublished project history or the pinned host overlay needed to deploy B1 directly.
4. The public stage outline is in `RESTORE_AND_BENCH.md`. Any live deployment needs operator approval and physical isolation.

Binary DataFlash, raw TX/journal evidence, exact host configuration and identities, complete fixture trees, and site-specific deployment/rollback files are kept in a protected local archive and are not part of this public backup.
