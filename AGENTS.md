# Instructions for long-running Codex / ChatGPT Work development

## Objective
Produce an actually usable, integrated Windows application, not a stream of commits or progress notes. Keep working through multiple coherent implementation, integration, regression and root-cause-fix cycles in the *same available work session*. One modified file, one test or one commit is not a completion criterion.

## Mandatory session workflow
1. Check live branch HEAD, draft PR and `docs/DEVELOPMENT_STATE.md` (if present), `docs/CONTROLLED_INTEGRATION.md`, actual tests and known blockers.
2. Preserve existing user work and prior architectural decisions. Work on this branch; never modify `main`, silently create a new competing PR, merge, or release.
3. Prioritize an end-to-end vertical slice across implementation, persistence, lifecycle, UI and failure paths. Detect common root causes rather than patching each repeated symptom.
4. Write/modify real code and meaningful regression tests. Run full available local regression, then integration, restart, concurrency and failure recovery scenarios. Fix and rerun. Clearly distinguish isolated tests from full checkout regression and actual Windows E2E.
5. Keep a short durable source-SHA-linked checkpoint with commands/results, integrated capabilities, unresolved blockers and the next exact step. On resumed sessions, use it rather than restarting analysis.
6. Use remaining session capacity for the next linked development step. Do not stop after creating a note, PR comment or a single commit.
7. Do not spend on GitHub Actions, API providers or other services without explicit authorization. Do not commit credentials. If a particular test runner/provider is unavailable, keep making independently verifiable progress and mark unrun tests honestly.
8. No interim builds, fake successful acceptance or routine reports. Final release requires comprehensive Windows clean-unpack GUI E2E and project-specific acceptance gates.

## Security and independence
The software's hard safety boundaries are not configurable by AI. Maintain independent projects and avoid changes to unrelated repositories. Disable potentially dangerous production actions until separately verified and explicitly authorized.

## Current development target: Bot Control Center Pro
Complete the Modern Qt Windows application with fully integrated common Market Data Hub/worker lifecycle, VELES multi-coin RSI bands, composable entry and exit, GRID, Pump & Dump, Polymarket PAPER, per-bot accounting and cross-bot risk, MY_BOTS inventory, trading history, exports/imports and recovery. Scale by actual machine resources rather than a fixed numeric bot ceiling.
Start with current consolidated draft PR #11 and its checkpoint; stabilize architectural boundaries and verify actual integration and clean-unpack Windows Qt E2E. Prioritize workflows creating/editing/starting/stopping bots, market data, trades, persistence, restart, and global monitoring. Randomized nonduplicative regression tasks and systemic defects collection. No release until mandatory end-to-end acceptance; no real trading unless separately authorized.
