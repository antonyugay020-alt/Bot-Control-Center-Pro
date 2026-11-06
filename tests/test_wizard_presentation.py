"""Qt-independent checks for the approved wizard's truthful summary rail."""

from bcc.config import defaults
from bcc.wizard_presentation import summary_html, summary_rows


def test_polymarket_summary_is_generated_from_saved_seconds_contract():
    config = defaults("POLYMARKET")
    config["polymarket"].update(
        entry_signal_mode="before_close",
        entry_seconds_before_close=47.5,
        entry_window_seconds=8.25,
        entry_min_seconds_left=5,
        impulse=0.12,
    )
    rows = dict(summary_rows(config))
    assert rows["Время входа"] == "За 47.5 сек до закрытия"
    assert rows["Окно входа"] == "8.25 сек"
    assert rows["Защитный порог"] == "5 сек"
    assert rows["Режим"] == "PAPER"


def test_summary_renders_safe_user_name_with_contrast_in_both_themes():
    config = defaults("POLYMARKET")
    config["name"] = "<font color='white'>bad</font>"
    for light in (False, True):
        html = summary_html(config, light=light)
        assert "&lt;font" in html
        assert "<font" not in html
        assert ("#19344b" if light else "#ecf5ff") in html
        assert ("#49647a" if light else "#a1b6cf") in html


def test_veles_summary_exposes_actual_financial_parameters():
    cfg = defaults("VELES")
    cfg["settings"].update(margin_mode="ISOLATED", leverage=5)
    cfg["settings"]["orders"].update(count=8, martingale_pct=12)
    rows = dict(summary_rows(cfg))
    assert rows["Маржа"] == "Isolated"
    assert rows["Плечо"] == "5×"
    assert rows["Ордера DCA"] == "8"
    assert rows["Мартингейл"] == "12%"


def test_dormant_seconds_are_visible_but_marked_inactive():
    from bcc.config import defaults
    cfg = defaults("POLYMARKET")
    cfg["polymarket"]["entry_seconds_before_close"] = 47.5
    html = summary_html(cfg)
    assert "В любое время" in html
    assert "47.5 сек" in html
    assert "не действует" in html


def test_effective_market_preview_explains_inherited_schedule_without_fake_quotes():
    from bcc.config import defaults
    from bcc.wizard_presentation import market_preview_rows, market_preview_html
    cfg = defaults("POLYMARKET")
    cfg["polymarket"].update(entry_signal_mode="before_close",
                             entry_seconds_before_close=47.5,
                             entry_window_seconds=8,entry_min_seconds_left=5)
    cfg["polymarket"]["overrides"]["ETH:300"]={"entry_seconds_before_close": 52.25}
    row = dict(market_preview_rows(cfg, "ETH", 300))
    assert row["Время"] == "от 52.25 до 44.25 сек до закрытия"
    assert "Рыночный сигнал" not in row["Проверка правила"]
    assert "котиров" not in market_preview_html(cfg, "ETH", 300).lower()
    assert "52.25" in market_preview_html(cfg, "ETH", 300)


def test_invalid_market_rule_reported_without_fake_green_status():
    from bcc.config import defaults
    from bcc.wizard_presentation import market_preview_rows
    cfg = defaults("POLYMARKET")
    cfg["polymarket"].update(entry_signal_mode="before_close",
                             entry_seconds_before_close=12,
                             entry_window_seconds=25)
    assert dict(market_preview_rows(cfg))["Проверка правила"].startswith("Ошибка:")


def test_anytime_market_preview_uses_explicit_mode_label_and_cutoff():
    """The wizard preview and summary must agree on the actual entry mode."""
    from bcc.config import defaults
    from bcc.wizard_presentation import market_preview_rows, market_preview_html
    cfg = defaults("POLYMARKET")
    cfg["polymarket"].update(entry_signal_mode="anytime",
                              entry_seconds_before_close=47.5,
                              entry_min_seconds_left=5)
    preview = dict(market_preview_rows(cfg, "BTC", 300))
    assert "В любое время" in preview["Время"]
    assert "расписания" in preview["Время"]
    assert "5 сек" in preview["Защитный порог"]
    assert "47.5" not in preview["Время"]  # Dormant seconds are not active.
    assert "В любое время" in market_preview_html(cfg, "BTC", 300)


def test_veles_summary_counts_parent_sections_separately_from_ranges():
    """Summary must reflect the two-level VELES tree, not flat engine rows."""
    from bcc.section_hierarchy import grouped_ranges, adopt_group
    cfg = defaults("VELES")
    cfg["name"] = "VELES group count"
    rows = dict(summary_rows(cfg))
    assert rows["Секции"] == "1"
    assert rows["Диапазоны RSI"] == "2"
    # Explicit parent metadata must produce the same author-visible counts.
    adopt_group(cfg["sections"], grouped_ranges(cfg["sections"])[0], "BTC RSI")
    rows = dict(summary_rows(cfg))
    assert rows["Секции"] == "1"
    assert rows["Диапазоны RSI"] == "2"


def test_veles_summary_counts_separate_groups_when_incompatible():
    cfg = defaults("VELES")
    cfg["sections"][1]["side"] = "SHORT"
    rows = dict(summary_rows(cfg))
    assert rows["Секции"] == "2"
    assert rows["Диапазоны RSI"] == "2"
