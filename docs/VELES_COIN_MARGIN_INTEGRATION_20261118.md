# VELES per-coin margin mode — integrated source checkpoint, NOT RELEASE

2026-11-18. Base source commit `7bba4909235cc56e0777075422c705a948eb44c2`.
Development branch `codex/ws-transport-isolated-20261113`.

## Architectural change

A VELES section can now choose **percent of its bot deposit** or a
**fixed USDT margin amount**. This is margin capital *before* leverage
and not a nominal order size. Both settings use the same public config
contract, not a GUI-only approximation.

- `bcc/config.py`: `allocation_mode` = `percent` or `fixed_usdt`,
  `allocation_pct` and `allocation_usdt`; strict finite numeric validation,
  defaults preserving existing 5%-of-capital behavior; read-time
  migration of older config objects without rewriting their source files.
  `effective()` already merges bot defaults, per-coin overrides,
  per-section overrides, and coin+section overrides in that order.
  Shared `section_margin_budget()` resolves exactly one active mode.
- `bcc/strategies.py`: VELES PAPER engine now uses the shared sizing
  function; reinvested capital affects only percent mode. All current
  admission, liquidation protection, shared risk, fee and cash gates
  remain in force. This change never authorizes real exchange orders.
- `bcc/widgets.py`: Modern Qt VELES constructor offers a mode selector
  and two reversible input fields, activating the selected one.
  Other strategies are not assigned an unsupported fixed-margin mode.
- `bcc/wizard_presentation.py`: global mode shown in summary;
  per-coin/per-range grid preview uses the same sizing rule as
  the PAPER engine, not an independent percentage formula.
- `tests/test_veles_capital_per_coin_mode.py`: legacy settings,
  percent/fixed math, per-coin and per-section isolation,
  invalid/NaN/inf rejection, preview consistency, undo/edit
  and visible mode tests.
- `tests/test_veles_fixed_capital_qt.py`: Qt create/save/reopen,
  mode-dependent controls, independent per-coin persistence, and
  legacy percent reopen. Must run on actual Windows Qt.

## Intentionally unchanged

The older observed 19-position cap has not been reproduced from live
telemetry. `bcc/shared_risk.py` already has `max_positions=0` as
the uncapped default; this change does not guess what blocked the
user's 20th position. It does not alter Polymarket, GRID or Pump&Dump
sizing policies. A fixed amount is **per selected coin AND VELES section**
and is independently subject to available balance. It is not a
total per-coin budget shared across multiple simultaneously active
sections; that would require separate portfolio allocation semantics.

## QA and source-integrity gates

This commit is a DEVELOPMENT source change, not a sealed Windows release.
The existing `SHA256_MANIFEST.json` and previously tested immutable ZIP
must not be represented as covering new files. Before Windows acceptance,
the exact resulting source must be sealed using `tools/seal_source.py`
with a new unique internal root, unpacked into a new independent QA
directory, then verified with `tools/verify_package.py`. Never modify
live bot folders or install into an active Windows runtime.

Run complete automated regression, native Modern Qt constructor/persistence
acceptance, actual public Bybit WS/Hub/SQLite tests, the manual held-position
Stop/Restart/GUI-reopen workflow, then compare the final source checksum.
Do not call Linux offscreen Qt native acceptance, and do not publish a
release until all mandatory gates pass. No paid providers or GitHub Actions
authorized.

**Execution evidence at checkpoint:** GitHub file updates and exact
commit diff verified. No Python regression or native Windows Qt was run
in this chat's environment; neither is claimed PASS. The previous
independent Windows offline Kie gateway 257 PASS belongs to another
branch and has no bearing on this BCC product change.
