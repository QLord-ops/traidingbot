import numpy as np
import pandas as pd
from src.backtest import run_backtest
from src.config import Settings


def make_df(n, freq):
    times = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    base = 100 + np.linspace(0, 30, n) + np.sin(np.arange(n) / 3) * 2
    return pd.DataFrame({
        "open_time": times,
        "close_time": times + pd.Timedelta(freq),
        "open": base,
        "high": base + 1.5,
        "low": base - 1.5,
        "close": base + 0.5,
        "volume": np.where(np.arange(n) % 7 == 0, 300, 100),
    })


def test_backtest_runs_without_future_merge():
    settings = Settings()
    result = run_backtest(make_df(500, "15min"), make_df(500, "1h"), settings)
    assert result.initial_balance == 1000
    assert result.final_balance > 0
    assert result.trades >= 0
