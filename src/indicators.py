from __future__ import annotations
import pandas as pd


def add_indicators(
    df: pd.DataFrame,
    ema_fast: int,
    ema_slow: int,
    ema_trend: int,
    atr_period: int,
    volume_period: int,
) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = out["close"].ewm(span=ema_fast, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=ema_slow, adjust=False).mean()
    out["ema_trend"] = out["close"].ewm(span=ema_trend, adjust=False).mean()

    prev_close = out["close"].shift(1)
    true_range = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr"] = true_range.ewm(alpha=1 / atr_period, adjust=False).mean()
    out["atr_pct"] = out["atr"] / out["close"]
    out["volume_avg"] = out["volume"].rolling(volume_period).mean()
    return out
