import dataclasses

import pandas as pd
import pytest

from src import macro_calendar
from src.backtest import BacktestParams, run_backtest
from src.config import Settings
from tests.conftest import make_df
from tests.test_donchian import donchian_settings, make_breakout_df

NO_COSTS = BacktestParams(taker_fee_rate=0.0, maker_fee_rate=0.0,
                          slippage_rate=0.0, apply_funding=False)


def test_in_blackout_window_boundaries():
    events = [(pd.Timestamp("2026-03-18 18:00", tz="UTC"), "FOMC")]
    assert macro_calendar.in_blackout("2026-03-18 12:00", 8, 2, events) == \
        "FOMC 2026-03-18 18:00"
    assert macro_calendar.in_blackout("2026-03-18 10:00", 8, 2, events)  # ровно −8h
    assert macro_calendar.in_blackout("2026-03-18 20:00", 8, 2, events)  # ровно +2h
    assert macro_calendar.in_blackout("2026-03-18 09:59", 8, 2, events) is None
    assert macro_calendar.in_blackout("2026-03-18 20:01", 8, 2, events) is None
    assert macro_calendar.in_blackout("2026-03-17 12:00", 8, 2, events) is None


def test_real_calendar_contains_known_events():
    # решение FOMC 18.06.2025 18:00 UTC и CPI 15.07.2025 12:30 UTC
    assert macro_calendar.in_blackout("2025-06-18 17:00", 8, 2) is not None
    assert macro_calendar.in_blackout("2025-07-15 12:00", 8, 2) is not None
    # отменённый CPI 13.11.2025 в календаре отсутствует
    assert macro_calendar.in_blackout("2025-11-13 13:00", 2, 2) is None


def test_upcoming_and_stale():
    events = macro_calendar.upcoming_events(now="2026-07-10", days=8)
    assert any(label == "CPI" for _, label in events)  # CPI 14.07.2026
    assert macro_calendar.calendar_is_stale(now="2026-11-20") is True
    assert macro_calendar.calendar_is_stale(now="2026-07-15") is False


def test_backtest_blocks_entry_in_blackout(monkeypatch):
    settings = dataclasses.replace(donchian_settings(), macro_filter=True,
                                   macro_block_before_h=8, macro_block_after_h=2)
    settings.validate()
    df = make_breakout_df(crash_at=280)
    trend = make_df(120, freq="1h", slope=0.2)
    # событие точно в момент входа (open свечи после пробойной)
    baseline = run_backtest(df, trend, settings, NO_COSTS)
    assert baseline.trades >= 1
    entry_time = pd.Timestamp(baseline.trade_log[0].entry_time)
    monkeypatch.setattr(macro_calendar, "EVENTS", [(entry_time, "CPI")])
    filtered = run_backtest(df, trend, settings, NO_COSTS)
    assert filtered.skipped_by_macro >= 1
    assert filtered.trades < baseline.trades or (
        filtered.trades == baseline.trades
        and filtered.trade_log[0].entry_time != baseline.trade_log[0].entry_time
    )


def test_macro_filter_disabled_by_flag(monkeypatch):
    settings = dataclasses.replace(donchian_settings(), macro_filter=False)
    settings.validate()
    df = make_breakout_df(crash_at=280)
    trend = make_df(120, freq="1h", slope=0.2)
    baseline = run_backtest(df, trend, settings, NO_COSTS)
    entry_time = pd.Timestamp(baseline.trade_log[0].entry_time)
    monkeypatch.setattr(macro_calendar, "EVENTS", [(entry_time, "CPI")])
    unfiltered = run_backtest(df, trend, settings, NO_COSTS)
    assert unfiltered.skipped_by_macro == 0
    assert unfiltered.trades == baseline.trades


def test_config_validates_macro_windows():
    with pytest.raises(ValueError):
        dataclasses.replace(Settings(), macro_block_before_h=100).validate()
