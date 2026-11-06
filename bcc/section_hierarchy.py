"""VELES editor hierarchy layered over the existing flat execution contract.

The trading engine still addresses each RSI range by its original section ID.
Parent metadata is only saved when the user changes a group or adds a range.
An unedited legacy config is left byte-for-byte equivalent after round-trip.
"""

from copy import deepcopy
import math
import uuid

PARENT_ID = "range_parent_id"
PARENT_LABEL = "range_parent_label"


def rsi_bounds(section):
    """Return (entry, exit, entry_tf, exit_tf) for simple RSI cross ranges."""
    entry = section.get("entry", {}).get("conditions", [])
    exit_ = section.get("exit", {}).get("conditions", [])
    if len(entry) != 1 or len(exit_) != 1:
        return None
    a, b = entry[0], exit_[0]
    if (a.get("indicator") != "RSI" or b.get("indicator") != "RSI"
            or a.get("comparison") != b.get("comparison")
            or a.get("comparison") != ("cross_up" if section.get("side") == "LONG" else "cross_down")
            or a.get("trigger") not in ("bar_close", "intrabar")
            or b.get("trigger") not in ("bar_close", "intrabar")):
        return None
    try:
        return (float(a["value"]), float(b["value"]), int(a["timeframe"]), int(b["timeframe"]))
    except (KeyError, ValueError, TypeError):
        return None


def grouped_ranges(sections):
    """Ordered editor groups; do not mutate old sections or infer trading rules."""
    groups = []
    group_by_id = {}
    previous = None
    for section in sections:
        explicit = section.get(PARENT_ID)
        if explicit:
            group_id = explicit
            group_name = section.get(PARENT_LABEL, "Секция RSI")
        else:
            bounds = rsi_bounds(section)
            if (previous is not None and previous.get("inferred")
                    and bounds is not None and previous.get("last_bounds") is not None
                    and previous["side"] == section.get("side")
                    and previous["last_bounds"][1] == bounds[0]
                    and previous["last_bounds"][2:] == bounds[2:]
                    and previous["last_triggers"] == (
                        section["entry"]["conditions"][0]["trigger"],
                        section["exit"]["conditions"][0]["trigger"])
                    and previous["last_settings"] == section.get("overrides", {})
                    and previous["last_enabled"] == section.get("enabled")):
                group_id = previous["id"]
                group_name = previous["name"]
            else:
                group_id = "legacy:" + section["id"]
                group_name = (f"{section['side']} · RSI" if bounds is not None
                              else section.get("label", "Секция"))
        found = group_by_id.get(group_id)
        if found is None:
            found = dict(id=group_id, name=group_name, side=section.get("side"),
                         sections=[], inferred=not bool(explicit), last_bounds=None,
                         last_triggers=None, last_settings=None, last_enabled=None)
            groups.append(found)
            group_by_id[group_id] = found
        found["sections"].append(section)
        found["last_bounds"] = rsi_bounds(section)
        if found["last_bounds"] is not None:
            found["last_triggers"] = (
                section["entry"]["conditions"][0]["trigger"],
                section["exit"]["conditions"][0]["trigger"])
        else:
            found["last_triggers"] = None
        found["last_settings"] = section.get("overrides", {})
        found["last_enabled"] = section.get("enabled")
        previous = found
    return groups


def adopt_group(sections, group, name=None):
    """Persist an explicit parent without changing existing execution IDs."""
    gid = group["id"]
    if gid.startswith("legacy:"):
        gid = "parent_" + uuid.uuid4().hex[:12]
    title = (name if name is not None else group["name"]).strip()
    for section in group["sections"]:
        section[PARENT_ID] = gid
        section[PARENT_LABEL] = title
    return gid


def replace_range(sections, section_id, edited):
    """Edit a child without accidentally splitting its legacy display parent.

    No hierarchy metadata is written on an unchanged round-trip. When the
    user actually changes a legacy range, its current inferred parent becomes
    explicit *before* the changed conditions/enabled state are applied.
    Original worker/SQLite range IDs are never changed.
    """
    if not isinstance(edited, dict) or edited.get("id") != section_id:
        raise ValueError("Нельзя изменять ID диапазона при редактировании")
    draft = deepcopy(sections)
    index = next((i for i, s in enumerate(draft) if s["id"] == section_id), None)
    if index is None:
        raise ValueError("Диапазон не найден")
    if draft[index] == edited:
        return draft
    group = next((g for g in grouped_ranges(draft)
                  if any(s["id"] == section_id for s in g["sections"])), None)
    if group is None:
        raise ValueError("Родительская секция не найдена")
    if group["inferred"] and len(group["sections"]) > 1:
        adopt_group(draft, group)
    changed = deepcopy(edited)
    # Parent metadata is authoritative; editing the child must not silently
    # move its position to another group or overwrite the parent's label.
    for key in (PARENT_ID, PARENT_LABEL):
        if key in draft[index]:
            changed[key] = draft[index][key]
        else:
            changed.pop(key, None)
    draft[index] = changed
    return draft


def append_rsi_range(sections, group_id, name, start, end, entry_tf=2, exit_tf=1):
    """Create a range inheriting its group's direction and PAPER risk settings.

    A group with compound signals cannot silently lose its filters: range
    insertion is limited to the simple RSI crossing contract.
    """
    # The editor limits these inputs, but callers importing or composing
    # rules programmatically must receive the same bounded RSI contract.
    if (any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 100
            for v in (start, end))):
        raise ValueError("Уровни RSI должны быть конечными числами от 0 до 100")
    from .config import TIMEFRAMES
    if (type(entry_tf) is not int or entry_tf not in TIMEFRAMES
            or type(exit_tf) is not int or exit_tf not in TIMEFRAMES):
        raise ValueError("Недопустимый таймфрейм RSI")
    parents = [s for s in sections if s.get(PARENT_ID) == group_id]
    if not parents:
        raise ValueError("Секция не найдена")
    original = parents[0]
    # Preserve other conditions on composite RSI + EMA / Volume groups.
    # Exactly one RSI entry and exit level must be identifiable.
    def rsi_condition(rule):
        matches = [c for c in rule.get("conditions", []) if c.get("indicator") == "RSI"]
        if len(matches) != 1:
            raise ValueError("В секции нужен один RSI для входа и один для выхода")
        return matches[0]
    entry_rule = rsi_condition(original["entry"])
    exit_rule = rsi_condition(original["exit"])
    side = original["side"]
    expected = "cross_up" if side == "LONG" else "cross_down"
    # Imported/previously edited groups may contain opposite RSI crossings.
    # A newly created standard range must follow its chosen LONG/SHORT side,
    # without modifying any existing range or inherited additional filters.
    # Do not reinterpret arbitrary comparison expressions as crossings.
    for rule in (entry_rule, exit_rule):
        if rule.get("comparison") not in ("cross_up", "cross_down"):
            raise ValueError("Для нового диапазона RSI требуется пересечение вверх или вниз")
    if (side == "LONG" and not start < end) or (side == "SHORT" and not start > end):
        raise ValueError("Направление диапазона должно соответствовать LONG или SHORT")
    for s in parents:
        entry_levels = [c for c in s["entry"]["conditions"] if c.get("indicator") == "RSI"]
        exit_levels = [c for c in s["exit"]["conditions"] if c.get("indicator") == "RSI"]
        if (len(entry_levels) == len(exit_levels) == 1
                and (float(entry_levels[0]["value"]), float(exit_levels[0]["value"]))
                == (float(start), float(end))):
            raise ValueError("Такой RSI-диапазон уже существует в секции")
    candidate = deepcopy(original)
    candidate["id"] = "range_" + uuid.uuid4().hex[:12]
    candidate["label"] = f"{side} RSI {start:g}–{end:g}"
    a = rsi_condition(candidate["entry"])
    b = rsi_condition(candidate["exit"])
    a["comparison"] = expected
    b["comparison"] = expected
    a["value"] = float(start)
    b["value"] = float(end)
    a["timeframe"] = entry_tf
    b["timeframe"] = exit_tf
    candidate[PARENT_ID] = group_id
    candidate[PARENT_LABEL] = name
    sections.append(candidate)
    return candidate
