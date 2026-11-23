"""Exchange inventory and configuration must accept the same symbol identifiers."""

import pytest
from test_core import config

from bcc.config import valid_symbol, validate
from bcc.market import PublicData


def inventory_row(symbol, **overrides):
    return {
        "symbol": symbol,
        "status": "Trading",
        "quoteCoin": "USDT",
        "contractType": "LinearPerpetual",
        **overrides,
    }


def test_inventory_preserves_single_letter_and_unicode_perpetuals_and_skips_delivery(
    monkeypatch,
):
    pages = [
        {
            "list": [
                inventory_row("BTCUSDT"),
                inventory_row("AUSDT"),
                inventory_row("BTCUSDT-25DEC26", contractType="LinearFutures"),
            ],
            "nextPageCursor": "page2",
        },
        {
            "list": [
                inventory_row("币安人生USDT"),
                inventory_row("AUSDT"),
                inventory_row("ETHUSDC", quoteCoin="USDC"),
                inventory_row("STOPUSDT", status="Settled"),
            ],
            "nextPageCursor": "",
        },
    ]
    source = PublicData()
    calls = []

    def read(route, params):
        calls.append(params.copy())
        return {"result": pages[len(calls) - 1]}

    monkeypatch.setattr(source, "bybit", read)
    try:
        symbols = source.list_symbols()
        assert set(symbols) == {"BTCUSDT", "AUSDT", "币安人生USDT"}
        assert calls[1]["cursor"] == "page2"
        cfg = config()
        cfg["symbols"] = symbols
        assert validate(cfg)["symbols"] == symbols
    finally:
        source.close()


@pytest.mark.parametrize(
    "symbols,message",
    [
        (["BTCUSDT", "BTCUSDT"], "Повторяющиеся торговые пары: BTCUSDT"),
        (["BTCUSDT-25DEC26"], "Неверный формат торговых пар"),
        (["../BTCUSDT"], "Неверный формат торговых пар"),
        ([12], "Торговые пары должны быть строками"),
        ([], "Выберите хотя бы одну торговую пару"),
    ],
)
def test_symbol_validation_reports_the_actual_failure(symbols, message):
    cfg = config()
    cfg["symbols"] = symbols
    with pytest.raises(ValueError, match=message):
        validate(cfg)


@pytest.mark.parametrize(
    "symbol", ["AUSDT", "币安人生USDT", "1000000MOGUSDT", "BTCUSDT"]
)
def test_safe_perpetual_identifiers_are_supported(symbol):
    assert valid_symbol(symbol, "VELES")


def test_inventory_rejects_partial_pages_after_pagination_safety_cap(monkeypatch):
    """An incomplete Bybit scan must not be presented as a complete list."""
    from bcc.market import DataUnavailable

    source = PublicData()
    count = 0

    def read(route, params):
        nonlocal count
        count += 1
        return {"result": {"list": [inventory_row(f"COIN{count}USDT")],
                           "nextPageCursor": f"next-{count}"}}

    monkeypatch.setattr(source, "bybit", read)
    try:
        with pytest.raises(DataUnavailable, match="полный список пар"):
            source.list_symbols()
        assert count == 30
    finally:
        source.close()


def test_configured_symbol_inventory_has_no_artificial_5000_ceiling():
    # Data schemas must not reject assets solely because the operator manages
    # a very large portfolio. A provider may still reject unavailable symbols
    # at the transport boundary; admission never promises unlimited feeds.
    cfg = config()
    symbols = [f"COIN{i}USDT" for i in range(12000)]
    cfg["symbols"] = symbols
    assert validate(cfg)["symbols"] == symbols


def test_large_duplicate_inventory_reports_first_few_without_quadratic_scan():
    cfg = config()
    cfg["symbols"] = ["BTCUSDT"] + [f"COIN{i}USDT" for i in range(7000)] + ["BTCUSDT"]
    with pytest.raises(ValueError, match="Повторяющиеся торговые пары: BTCUSDT"):
        validate(cfg)
