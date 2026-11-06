"""Pure pre-save projection for the Qt bot constructor.

Keep the editor's uncommitted override drafts in memory while a user toggles
coins, durations or sections. Only remove abandoned overrides from the *saved*
configuration. The repository still refuses to drop live PAPER positions.
"""

from copy import deepcopy


def selected_configuration(config):
    result = deepcopy(config)
    removed = []
    symbols = set(result.get("symbols", []))
    section_ids = {s.get("id") for s in result.get("sections", [])
                     if isinstance(s, dict)}
    overrides = result.get("coin_overrides")
    if isinstance(overrides, dict):
        for symbol, patch in list(overrides.items()):
            if symbol not in symbols:
                removed.append(f"монета {symbol}")
                del overrides[symbol]
                continue
            if not isinstance(patch, dict):
                continue  # validator reports malformed structures
            section_overrides = patch.get("sections")
            if isinstance(section_overrides, dict):
                for section_id in list(section_overrides):
                    if section_id not in section_ids:
                        removed.append(f"секция {symbol}/{section_id}")
                        del section_overrides[section_id]
    if result.get("strategy") == "POLYMARKET":
        poly = result.get("polymarket")
        if isinstance(poly, dict) and isinstance(poly.get("overrides"), dict):
            valid = {f"{coin}:{duration}" for coin in symbols
                     for duration in poly.get("durations", [])}
            for key in list(poly["overrides"]):
                if key not in valid:
                    removed.append(f"рынок {key}")
                    del poly["overrides"][key]
    return result, removed
