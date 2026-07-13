import dataclasses

import pytest

from src.backtest import BacktestParams, run_backtest
from src.config import Settings
from src.testnet_engine import EngineError, TestnetEngine
from tests.conftest import make_df

NO_COSTS = BacktestParams(taker_fee_rate=0.0, maker_fee_rate=0.0,
                          slippage_rate=0.0, apply_funding=False)


def donchian_settings(**overrides) -> Settings:
    fields = {"strategy_mode": "donchian", "donchian_period": 20,
              "trail_atr_mult": 3.0}
    fields.update(overrides)
    settings = dataclasses.replace(Settings(), **fields)
    settings.validate()
    return settings


def make_breakout_df(n=400, breakout_at=250, crash_at=None):
    """Плоский рынок, затем пробой канала вверх; опционально обвал позже."""
    df = make_df(n, slope=0.0)  # боковик: канал узкий и стабильный
    # восходящий дрейф после пробоя, чтобы трейлинг-стоп подтягивался
    for i in range(breakout_at, n):
        lift = 3.0 + (i - breakout_at) * 0.3
        for col in ("open", "high", "low", "close"):
            df.loc[i, col] += lift
    if crash_at is not None:
        drop = 25.0
        for col in ("open", "high", "low", "close"):
            df.loc[crash_at:, col] -= drop
    return df


def test_donchian_long_breakout_opens_trade():
    settings = donchian_settings()
    df = make_breakout_df(crash_at=280)
    trend = make_df(120, freq="1h", slope=0.2)  # старший тренд вверх
    result = run_backtest(df, trend, settings, NO_COSTS)
    assert result.trades >= 1
    trade = result.trade_log[0]
    assert trade.side == "LONG"
    assert trade.take_profit is None  # выход только по трейлинг-стопу
    assert trade.reason in ("STOP", "STOP_GAP")


def test_trailing_stop_locks_in_profit():
    """После длинного роста трейлинг-стоп выше входа: выход в плюс."""
    settings = donchian_settings()
    df = make_breakout_df(n=500, breakout_at=250, crash_at=400)
    trend = make_df(150, freq="1h", slope=0.2)
    result = run_backtest(df, trend, settings, NO_COSTS)
    assert result.trades >= 1
    first = result.trade_log[0]
    assert first.exit > first.entry  # стоп подтянулся выше цены входа
    assert first.pnl_usdt > 0


def test_no_entry_against_trend():
    """Пробой вверх при нисходящем старшем тренде игнорируется."""
    settings = donchian_settings()
    df = make_breakout_df(crash_at=None)
    trend = make_df(120, freq="1h", slope=-0.5)  # тренд вниз
    result = run_backtest(df, trend, settings, NO_COSTS)
    assert all(t.side == "SHORT" for t in result.trade_log)


def test_flat_market_no_trades():
    settings = donchian_settings()
    df = make_df(400, slope=0.0)
    trend = make_df(120, freq="1h", slope=0.0)
    result = run_backtest(df, trend, settings, NO_COSTS)
    assert result.trades == 0


def test_engine_rejects_donchian_mode(tmp_path):
    from src.journal import Journal
    from tests.test_engine import FakeClient, make_testnet_settings
    settings = dataclasses.replace(make_testnet_settings(),
                                   strategy_mode="donchian")
    with pytest.raises(EngineError, match="donchian"):
        TestnetEngine(settings, FakeClient(), Journal(str(tmp_path / "t.db")))


def test_config_validates_donchian_params():
    with pytest.raises(ValueError):
        donchian_settings(donchian_period=5)
    with pytest.raises(ValueError):
        donchian_settings(trail_atr_mult=0)
    with pytest.raises(ValueError):
        dataclasses.replace(Settings(), strategy_mode="magic").validate()
