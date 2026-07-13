import numpy as np
import pandas as pd
from pandas.tseries.frequencies import to_offset


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
