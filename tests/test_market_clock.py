"""More precise diagnostics retain the existing execution freshness limits."""

import pytest

from bcc.market import DataUnavailable, check_book_time


@pytest.mark.parametrize("age", [-0.5, 0, 3])
def test_book_time_accepts_only_existing_freshness_window(monkeypatch, age):
    monkeypatch.setattr("bcc.market.time.time", lambda: 1000)
    check_book_time(1000 - age, "Bybit")


@pytest.mark.parametrize("age", [-0.6, 3.1])
def test_book_time_reports_clock_difference_without_allowing_old_execution(
    monkeypatch, age
):
    monkeypatch.setattr("bcc.market.time.time", lambda: 1000)
    with pytest.raises(DataUnavailable) as error:
        check_book_time(1000 - age, "Bybit")
    text = str(error.value)
    assert f"возраст=[{age:+.3f}, {age:+.3f}]" in text
    assert "локальное UTC=1000.000" in text and "время стакана UTC=" in text
    assert ("опережает сверенное UTC" in text) == (age < 0)
