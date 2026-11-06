"""Fail-safe settings for the sole Modern Qt dashboard (no Qt import)."""

DEFAULT_PREFERENCES = {"scale": 100, "refresh_ms": 2000, "theme": "dark"}


def normalize_preferences(value):
    """Treat the saved file as untrusted input, without modifying it.

    A damaged optional UI preference must never prevent loading PAPER bots.
    """
    defaults = DEFAULT_PREFERENCES.copy()
    if not isinstance(value, dict):
        return defaults
    for key, lower, upper in (("scale", 75, 150), ("refresh_ms", 500, 30000)):
        number = value.get(key)
        if type(number) is int and lower <= number <= upper:
            defaults[key] = number
    if value.get("theme") in ("dark", "light"):
        defaults["theme"] = value["theme"]
    return defaults
