# Bot Control Center Pro

Private source repository for the Windows Modern Qt Dashboard and independent PAPER trading-bot runtime.

**Status: INTERNAL DEVELOPMENT — NOT RELEASE-CERTIFIED.** The cumulative architectural line is [Draft PR #11](https://github.com/antonyugay020-alt/Bot-Control-Center-Pro/pull/11) on `codex/architecture-consolidation-20261107`. It integrates Market Data Hub producer epochs, multi-bot snapshot export, provider-policy fencing and reboot-safe refusal recovery. Subsequent commits on that branch extend the same architecture.

The release target includes VELES, Polymarket PAPER, GRID and PUMP&DUMP, a resource-bounded fleet without an artificial bot-count ceiling, transactional accounting, crash recovery and native Windows Qt end-to-end acceptance. **No clean Windows Qt E2E acceptance has been certified on the current head.** GitHub Actions failures without executed job steps are non-diagnostic and must not be presented as test failures or successes. Do not trigger paid reruns without a justified need.

Source integrity is guarded by `SHA256_MANIFEST.json` and `tools/verify_package.py`; `tools/seal_source.py` prepares a clean acceptance snapshot, not a user release. The manifest excludes itself to avoid a circular hash and includes this README.

Never commit trading credentials, live orders, `.env`, `MY_BOTS`, private SQLite databases or runtime logs. All development and automated tests must remain PAPER-only and offline unless a separate controlled public-market test is explicitly authorized.
