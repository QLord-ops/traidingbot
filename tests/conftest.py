import os

import numpy as np
import pandas as pd
import pytest
from pandas.tseries.frequencies import to_offset

# Все переменные, которые читает Settings: тесты не должны зависеть от
# локального .env пользователя (он предназначен для реального запуска).
_SETTINGS_ENV_VARS = [
    "BINANCE_API_KEY", "BINANCE_API_SECRET", "TRADING_MODE", "SYMBOLS",
    "SIGNAL_INTERVAL", "TREND_INTERVAL", "KLINE_LIMIT", "RISK_PER_TRADE",
    "DAILY_LOSS_LIMIT", "MAX_TRADES_PER_DAY", "MAX_OPEN_POSITIONS",
    "LEVERAGE", "REWARD_RISK", "STRATEGY_MODE", "DONCHIAN_PERIOD",
    "TRAIL_ATR_MULT", "EMA_FAST", "EMA_SLOW", "EMA_TREND", "ATR_PERIOD",
    "ATR_MIN_PCT", "VOLUME_PERIOD", "VOLUME_MULTIPLIER", "POLL_SECONDS",
    "LOG_LEVEL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "ENABLE_LIVE_ORDERS", "LIVE_CONFIRMATION", "WEB_HOST", "WEB_PORT",
]


@pytest.fixture(autouse=True)
def _clean_settings_env(monkeypatch):
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make_df(n: int, freq: str = "15min", start: str = "2026-01-01",
            slope: float = 0.05) -> pd.DataFrame:
    """Ровный восходящий тренд без сигналов (объём постоянный, пробоев нет)."""
    times = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    base = 100 + np.arange(n, dtype=float) * slope
    return pd.DataFrame({
        "open_time": times,
        "close_time": times + to_offset(freq),
        "open": base,
        "high": base + 0.6,
        "low": base - 0.6,
        "close": base + 0.5,
        "volume": np.full(n, 100.0),
    })


def add_volume_spike(df: pd.DataFrame, idx: int) -> None:
    """Всплеск объёма → LONG-сигнал на свече idx (в восходящем тренде
    long_score = 25 + 20 + 15 + 15(volume) + 10(atr) = 85 >= 75)."""
    df.loc[idx, "volume"] = 500.0


def add_crash(df: pd.DataFrame, idx: int, depth: float = 10.0,
              gap: bool = False) -> None:
    """Свеча idx задевает и SL и TP (low/high далеко в обе стороны).
    gap=True дополнительно открывает свечу ниже стопа."""
    df.loc[idx, "low"] = df.loc[idx, "open"] - depth
    df.loc[idx, "high"] = df.loc[idx, "open"] + depth
    if gap:
        df.loc[idx, "open"] = df.loc[idx, "open"] - depth / 2
        df.loc[idx, "low"] = df.loc[idx, "open"] - depth
