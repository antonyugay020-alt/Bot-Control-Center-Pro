# Bot Control Center Pro — controlled integration checkpoint

Status: **DEVELOPMENT / NOT RELEASE**. This is a durable work checkpoint, not an acceptance certificate.

## Baseline and source checkpoint

- Baseline reviewed: `d136ffa3204e3632e6f0955f47e0c57a3e81d955`, Draft PR #11, `codex/architecture-consolidation-20261107`.
- Latest source-and-regression commit for this milestone: `dc66d3dd2042f97caf006f73bab6420d7de8bd7f`. The final SHA-256 manifest and this checkpoint are sealed in later commits on the **same** PR branch; read the live PR HEAD before the next milestone.
- Integration objective: a crashed Hub producer must not revoke an unrelated or newly reclaimed producer merely because Windows reused its numeric PID.

## Completed integration edges

- `tools/market_hub_service.py`: authenticated readiness receipt includes the exact randomly generated `leader_owner` identity of the producer.
- `bcc/hub_supervisor.py`: the supervisor validates OS process identity **and** the current live SQLite lease owner before persisting readiness. Process records now include the owner generation; existing PID-only records remain readable but cannot authorize early deletion. Crash recovery deletes only an exact recorded owner, never `LIKE pid:%`.
- Regression coverage: generation collision and stale PID-only records, matching-owner reclamation, successor lease preservation, native Windows redirector identity/lease proof, current service readiness API and the context-managed SQLite schema tracer.
- No account, private API, trading order or LIVE execution path was modified.

## Evidence and independent defect inventory

- **Verified via GitHub:** source/test commits were accepted by the GitHub Contents API. The SHA-256 source inventory is updated and read back separately; a successful commit does **not** establish runtime correctness.
- **Code-level defect H1:** PID-prefix lease deletion could revoke a successor Hub generation after PID reuse. Fixed by exact owner identity; regression added. **Execution pending**.
- **Test-contract defect H2:** older readiness and SQLite tracing tests assumed pre-refactor APIs. Updated to exercise `acquire_leadership` and context-managed connections. **Execution pending**.
- **Test commands required (NOT RUN):** `python -m pytest -q tests/test_hub_exact_generation_recovery.py tests/test_hub_supervisor_lifecycle.py tests/test_r620_windows_hub_startup.py tests/test_r622_hub_readiness_order.py tests/test_r625_windows_redirector_hub_pid.py tests/test_shared_sqlite_schema_migration.py`; followed by full project pytest regression and clean-unpack native Windows Modern Qt E2E.
- **Why not run:** this automation runtime cannot clone the private GitHub repository into its isolated Python/container environment (GitHub hostname resolution unavailable); the GitHub connector supports source read/write but not test execution. Prior GitHub Actions runs failed before useful job steps; no unapproved paid rerun.
- **Acceptance:** blocked until all-source integrity audit, full Python regression, Windows process lifecycle/Qt E2E and independent review are actually completed. No release, no clean-run counter.

## Next coherent milestone

Run the above tests in an executable environment; reconcile failures as one lifecycle package, then validate restart/Stop/Start of multiple PAPER workers, Hub ownership, lease recovery, dashboard monitoring and independent SQLite state under clean Windows unpack. Continue from the current Draft PR #11 HEAD, not from main or a stale PR.
