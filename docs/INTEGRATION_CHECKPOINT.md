# Bot Control Center Pro — controlled integration checkpoint

**DEVELOPMENT / NOT RELEASE.** Same branch `codex/architecture-consolidation-20261107`, draft PR #11; no merge, release, paid CI or real orders.

## Source / resume

Session baseline: `ccaedaaa00fa36b7ad9aa6a63f5bdd04dd0cc75c`.
Verified/pushed milestones: `8007e25` (Hub schema admission), `c1faa10` (contract convergence / unready-child cleanup). The commit containing this checkpoint completes exact-owner startup-failure recovery; reread remote HEAD before continuing. Preserve concurrent user work.

Executable private checkout is available; the previous cloning/DNS blocker is resolved. Linux/Python 3.12.14, SQLite 3.53.1, PySide6 6.12.0, pytest 9.2.0. Virtualenv and transient evidence: `/workspace/work/`, outside the immutable checkout.

## Integrated fixes / independent defect inventory

- H1–H3: existing Hub boot/generation/readiness boundaries now executed in the full checkout. Native Windows acceptance still pending.
- H4–H6: restored fresh/legacy Hub startup (required-column set comparisons incorrectly used dictionaries); preserve writer-free WAL reader admission; validate exact single-column primary keys and nonunique/unconditional queue/expiry indexes. Repair owned indexes atomically without changing live subscription/leader tokens. Incompatible identities and foreign-owned index collisions roll back.
- H7: authenticate and retain the real OS child behind a Windows venv redirector before SQLite readiness checks. Failed readiness cannot orphan that child.
- H8: abrupt startup termination bypasses Hub.close(), leaving a 120-second lease. Cleanup now revokes only a proven-dead exact owner. If SQLite is still unavailable, retain pid/creation-time/exact-owner recovery metadata for next start. A live failed-startup witness cannot advertise readiness; AccessDenied cannot authorize revocation. Immediate retry and successor reuse are verified with real OS processes, including SIGKILL modeling Windows TerminateProcess. No PID-prefix deletion.
- H9: consolidated old tests had diverged from production token, no-redirect, observed-denial, publication and duplicate-export contracts. Reconciled test adapters/fixtures and preserved strict production checks. Repeated-origin import concurrency now uses a valid archive, while duplicate export selections remain rejected. Synthetic API-login tests use only private temporary storage.
- H10: baseline source manifest omitted AGENTS.md and docs/CONTROLLED_INTEGRATION.md. Complete inventory now sealed; all baseline content digests otherwise matched. Explicit spawn exercises independent-process migrations without Linux fork-from-Qt-thread hazards.

## Executed verification

All commands run from the complete checkout using `/workspace/work/venv/bin/python`. `QT_QPA_PLATFORM=offscreen` for Qt/pytest; all workspaces are disposable PAPER data. No live market orders, model API calls or user ledgers.

1. Baseline linked Hub migration/boot/owner/lifecycle suite: **15 failed**, common TypeError. After required-column fix: **15 passed**. Expanded schema/Hub suite: **23 passed**.
2. Full cycle1: `python -m pytest tests -q --timeout=180 --junitxml=/workspace/work/full-cycle1.xml`: **3333 passed, 17 failed**, no skipped cases (246.72s). Initial exploratory baseline full run was interrupted after 142 passed/4 failed; it is not complete evidence.
3. Affected-contract trial: 90 passed/2 new-test failures (empty list check), corrected; linked readiness/redirector group **8 passed**. Full cycle2: same full-suite command with `full-cycle2.xml`: **3360 passed, 0 skipped** (224.57s), four fork deprecation warnings subsequently addressed with explicit spawn.
4. New abrupt-stop / retained-owner recovery cases reproduced **2 failures / 7 passes** before the production fix. A first patch invocation exposed an indentation error during collection; corrected before verification. Expanded linked schema/boot/owner/readiness/real-process recovery suite: **34 passed** (6.71s), no warnings.
5. Final complete checkout: `python -m pytest tests -q --timeout=180 --junitxml=/workspace/work/full-cycle3.xml`: **3363 passed, 0 failed/errors/skipped/warnings**, 228.61s. No remaining Hub/runtime service processes after teardown.

Additional executable GUI check: `python -m bcc.gui_check --offscreen --output /workspace/work/gui-cycle2`: **PASS**. Eight actual Qt wizard create/edit/reopen and PAPER deposit-adjustment workflows; four core strategy copy/transfer/CSV/command-revoke/delete-isolation cycles; 20-row background global monitor; coin catalog and theme contrast. A bounded diagnostic invocation also exited 0. GUI command checks here are intent-only; actual worker Stop/Restart and synthetic strategy fills are independently covered by pytest. Linux offscreen is not Windows E2E.

Full raw JUnit/logs remain in `/workspace/work/`; durable counts, failed identities, environment, tested source fingerprint and GUI result are in `docs/REGRESSION_20261107.json`. Its tested source map refers to the pre-documentation evidence snapshot, not to subsequently edited evidence itself. Final documentation and manifest are verified separately. Compileall and git diff --check pass. No release archive was generated outside isolated tests.

## Outstanding acceptance / precise next step

No known failing Linux regression remains. Independent review and clean-unpack native Windows Modern Qt E2E remain **NOT RUN / NOT CERTIFIED**; live provider availability remains unverified. The attached executor is Linux and cannot certify native Windows behavior. Do not merge or release, enable real trading, or rerun paid Actions.

Next: on a native Windows clean source snapshot of this exact branch HEAD, verify all source hashes, then run `python -m tools.verify_windows --output <external-evidence-directory>`. Inspect complete collected/merged test inventory, GUI screenshots, real PAPER multi-worker Stop/Restart, abrupt Hub startup/recovery, global monitoring and ledger audits. Classify environment/provider denial separately from code failure; fix common causes and repeat affected plus complete acceptance. Keep PR #11 draft until all project-specific gates and independent review pass.
