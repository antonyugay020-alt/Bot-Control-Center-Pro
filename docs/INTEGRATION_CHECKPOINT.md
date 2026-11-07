# Controlled integration checkpoint — 2026-11-07

DEVELOPMENT / NOT RELEASE. Branch `codex/architecture-consolidation-20261107`, draft PR #11.

Baseline: `ccaedaaa00fa36b7ad9aa6a63f5bdd04dd0cc75c`. This milestone is identified by the Git commit containing this checkpoint; reread remote HEAD before resuming. No merge, releases, Actions dispatch or real orders.

Executable checkout is now available (the former DNS/clone blocker is resolved).
Linux, Python 3.12.14, PySide6 6.12.0, pytest 9.2.0, SQLite via Python.
Dependencies installed in `/workspace/work/venv`, outside immutable source.

## Implemented and verified

Hub schema admission previously raised TypeError for set <= dict, preventing fresh and migrated registries from opening. Compare required columns with dictionary keys. Verify the entire primary key, rejecting composite identities before claiming readiness; verify queue/expiry indexes are nonunique and unconditional. Repair malformed owned indexes atomically while preserving live leader/subscription generations. Reject incompatible identities with complete migration rollback.

`QT_QPA_PLATFORM=offscreen /workspace/work/venv/bin/python -m pytest -q tests/test_shared_sqlite_schema_migration.py tests/test_hub_boot_epoch_recovery.py tests/test_hub_exact_generation_recovery.py tests/test_hub_supervisor_lifecycle.py --timeout=180 --junitxml=/workspace/work/schema-cycle2.xml`: **23 passed**. Includes independent-process migrations, writer-held read-only admission, real concurrent Hub start/crash recovery and eight new schema-contract cases. Initial exact-baseline linked suite: **15 failed** with the shared TypeError; after the column-key correction: **15 passed**.

The baseline manifest omitted AGENTS.md and docs/CONTROLLED_INTEGRATION.md. All other baseline file digests matched. The manifest is regenerated from the complete inventory for this commit; no archive or release is produced.

## Full regression and defect inventory

After only the TypeError fix and manifest correction:
`QT_QPA_PLATFORM=offscreen /workspace/work/venv/bin/python -m pytest tests -q --timeout=180 --junitxml=/workspace/work/full-cycle1.xml`: **3333 passed, 17 failed, 0 skipped**, 246.72s. This is the full checkout, including offscreen real Qt and OS processes, not Windows E2E. Baseline exploratory full run was interrupted after 142 passed/4 failed once the shared startup defect was established; never count it as a complete run.

Open groups: stale Hub completion/service doubles; outdated public HTTP redirect/publication/observed-denial doubles; boot-change test using an expired successful-publication lease; conflicting duplicate export expectations; readiness test incorrectly accepting proof without a live SQLite generation; API-login test writing outside its private workspace; source verification now rejecting unknown files earlier than Git admission. Preserve strict production boundaries while reconciling tests. Exact failures retained in the next durable regression inventory.

## Next milestone / release gates

Resolve all 17 regressions by common contract, rerun affected groups, then the complete checkout. Add integrated fault/restart checks where failures expose missing behavior. Keep source manifest synchronized and push checkpoint commits with `[skip ci]` to avoid automatic paid Actions. Windows clean-unpack Modern Qt E2E, independent review and live market availability remain NOT RUN / NOT CERTIFIED. This Linux environment cannot certify native Windows. No merge or release until those gates pass.
