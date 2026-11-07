# Bot Control Center Pro — controlled integration checkpoint

Status: **DEVELOPMENT / NOT RELEASE**. This is a durable checkpoint, not a test or release certificate.

## Branch and baseline

- Integration branch: `codex/architecture-consolidation-20261107`, Draft PR #11.
- Baseline read and reconciled: `e1535c9ae80054c1fc7f0916c608bd5ab302c82b` (prior checkpoint: Hub readiness bound to exact process generation).
- This milestone's source and regression checkpoint: `1b3e888c6db3981bcf8d5e625d45e63f81bd5b7b`; the manifest sealing commit follows on this same branch. **Read live HEAD at next run.**
- Protocol: `docs/CONTROLLED_INTEGRATION.md`. No separate architectural branch or user release.

## Integrated milestone: OS-boot-aware Market Data Hub lease lifecycle

- `bcc/market_hub.py`: shared Hub leader SQLite schema now includes a backward-compatible `boot_epoch` witness. Existing schema migration remains under `BEGIN IMMEDIATE`. A producer can reclaim a **previous-boot** leader even if a persisted wall-clock expiry remains in the future. A same-boot producer cannot steal a live leader.
- Leadership acquisition, token lookup, renewal and subscription acknowledgement share the same boot-compatibility fence. Unknown/legacy boot witnesses retain the original bounded lease rather than allowing speculative takeover. An old leader token cannot acknowledge new subscriptions after boot-aware reclamation.
- `bcc/hub_supervisor.py`: process reuse, missing-record recovery and child-authored readiness all consult `HubRegistry.live_leader()` instead of treating a future wall-clock expiry as proof of a live producer. Existing exact PID/process-generation checks remain mandatory.
- `tests/test_hub_boot_epoch_recovery.py`: new offline regression for reboot with future expiry, same-boot exclusion, unknown witness, legacy leader schema migration, old-token renewal/completion refusal. `tests/test_shared_sqlite_schema_migration.py`: asserts the additional leader column during atomic migration.
- No trading credentials, account records, LIVE execution or exchange requests were changed.

## Test evidence and independent defect inventory

- **Defect H3:** a leader row persisted across an OS reboot could block new Hub startup until its wall-clock TTL expired, despite the producer being dead. The new code applies a boot witness at all leader admission boundaries. **Source committed; execution of repository tests pending.**
- **Defect H1/H2 from prior milestone:** exact owner recovery and updated readiness contracts remain integrated; full execution still pending.
- **Executed isolated check:** Linux Python 3.13.5, SQLite 3.46.1, in-memory reproduction of the new SQLite `INSERT ... ON CONFLICT` admission statement. Verified old-boot initial claim, same-boot takeover refusal and new-boot takeover with fresh token. This tests SQL behavior, **not** the checked-out repository or its pytest suite.
- **GitHub readback:** source and regression changes are committed to Draft PR #11. Source SHA-256 manifest must be resealed and independently read back after this checkpoint; no code commit alone constitutes acceptance.
- **Not executed:** `python -m pytest -q tests/test_hub_boot_epoch_recovery.py tests/test_shared_sqlite_schema_migration.py tests/test_hub_exact_generation_recovery.py tests/test_hub_supervisor_lifecycle.py tests/test_r620_windows_hub_startup.py tests/test_r622_hub_readiness_order.py tests/test_r625_windows_redirector_hub_pid.py`, full project pytest, and native Windows Modern Qt E2E.
- **Environment blocker:** this runtime cannot resolve github.com for cloning the private repository, and has no GitHub credential available to the isolated Python environment. The GitHub connector supports source read/write but not Python execution. No paid CI reruns or API calls were requested.
- **Release gate:** full source integrity audit, all-regression acceptance, clean-unpack Windows Qt GUI lifecycle tests and independent review remain **NOT RUN / NOT CERTIFIED**. No release or clean-run counter.

## Next coherent milestone

Run boot-epoch and previous Hub recovery tests together in an executable checkout. Reconcile any failures across HubRegistry, supervisor and service lifecycle as one integration unit, then perform multiple independent PAPER worker Stop/Restart, Market Data Hub recovery and Windows Modern Qt monitoring E2E. Preserve the exact-source SHA-256 manifest and continue from current PR #11 HEAD.
