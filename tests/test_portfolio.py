import dataclasses

import pytest

from src.backtest import BacktestParams
from src.portfolio import run_portfolio_backtest, signal_strength
from src.strategy import Side
from tests.conftest import make_df
from tests.test_donchian import donchian_settings, make_breakout_df

NO_COSTS = BacktestParams(taker_fee_rate=0.0, maker_fee_rate=0.0,
                          slippage_rate=0.0, apply_funding=False)


def test_single_symbol_portfolio_trades_like_backtest():
    settings = donchian_settings()
    df = make_breakout_df(crash_at=280)
    trend = make_df(120, freq="1h", slope=0.2)
    res = run_portfolio_backtest({"AAAUSDT": (df, trend, None)}, settings, NO_COSTS)
    assert res.trades >= 1
    assert res.trade_log[0].side == "LONG"
    assert "AAAUSDT" in res.trade_log[0].reason


def test_one_position_at_a_time():
    """Два символа с одновременными сигналами: занята только одна позиция."""
    settings = donchian_settings()
    df_a = make_breakout_df(crash_at=280)
    df_b = make_breakout_df(crash_at=280)
    trend = make_df(120, freq="1h", slope=0.2)
    res = run_portfolio_backtest(
        {"AAAUSDT": (df_a, trend, None), "BBBUSDT": (df_b.copy(), trend.copy(), None)},
        settings, NO_COSTS,
    )
    # позиции не пересекаются во времени
    intervals = [(t.entry_time, t.exit_time) for t in res.trade_log]
    for k in range(1, len(intervals)):
        assert intervals[k][0] >= intervals[k - 1][1]


def test_ranking_picks_stronger_trend():
    """При коллизии выбирается символ с более сильным старшим трендом."""
    settings = donchian_settings()
    df_weak = make_breakout_df(crash_at=280)
    df_strong = make_breakout_df(crash_at=280)
    trend_weak = make_df(120, freq="1h", slope=0.05)
    trend_strong = make_df(120, freq="1h", slope=0.8)  # цена дальше от EMA200
    res = run_portfolio_backtest(
        {"WEAKUSDT": (df_weak, trend_weak, None),
         "STRUSDT": (df_strong, trend_strong, None)},
        settings, NO_COSTS, rank_mode="trend",
    )
    assert res.trades >= 1
    assert "STRUSDT" in res.trade_log[0].reason


def test_signal_strength_modes():
    cur = {"close": 103.0, "donchian_high": 100.0, "donchian_low": 90.0, "atr": 2.0}
    trend_row = {"close": 110.0, "ema_trend": 100.0}
    assert signal_strength(cur, trend_row, Side.LONG, "breakout") == pytest.approx(1.5)
    assert signal_strength(cur, trend_row, Side.LONG, "trend") == pytest.approx(0.1)
    assert signal_strength(cur, trend_row, Side.LONG, "none") == 0.0


def test_portfolio_requires_donchian_mode():
    settings = dataclasses.replace(donchian_settings(), strategy_mode="score")
    with pytest.raises(ValueError):
        run_portfolio_backtest({}, settings, NO_COSTS)
