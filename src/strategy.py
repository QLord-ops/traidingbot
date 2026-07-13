from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import pandas as pd

from .config import Settings
from .indicators import add_indicators


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    entry: float | None
    stop: float | None
    take_profit: float | None
    score: int
    reason: str
    candle_time: str


def evaluate(symbol: str, signal_df: pd.DataFrame, trend_df: pd.DataFrame,
             settings: Settings) -> Signal:
    s = add_indicators(
        signal_df, settings.ema_fast, settings.ema_slow,
        settings.ema_trend, settings.atr_period, settings.volume_period
    )
    t = add_indicators(
        trend_df, settings.ema_fast, settings.ema_slow,
        settings.ema_trend, settings.atr_period, settings.volume_period
    )

    # Последняя строка может быть незакрытой свечой — используем последнюю закрытую.
    cur = s.iloc[-2]
    prev = s.iloc[-3]
    trend = t.iloc[-2]
    candle_time = str(cur["close_time"])

    long_score = 0
    short_score = 0
    reasons_long: list[str] = []
    reasons_short: list[str] = []

    if trend["close"] > trend["ema_trend"]:
        long_score += 25
        reasons_long.append("1H цена выше EMA200")
    elif trend["close"] < trend["ema_trend"]:
        short_score += 25
        reasons_short.append("1H цена ниже EMA200")

    if trend["ema_fast"] > trend["ema_slow"]:
        long_score += 20
        reasons_long.append("1H EMA20 выше EMA50")
    elif trend["ema_fast"] < trend["ema_slow"]:
        short_score += 20
        reasons_short.append("1H EMA20 ниже EMA50")

    if cur["ema_fast"] > cur["ema_slow"]:
        long_score += 15
        reasons_long.append("15m импульс вверх")
    elif cur["ema_fast"] < cur["ema_slow"]:
        short_score += 15
        reasons_short.append("15m импульс вниз")

    volume_ok = (
        pd.notna(cur["volume_avg"])
        and cur["volume"] >= cur["volume_avg"] * settings.volume_multiplier
    )
    if volume_ok:
        long_score += 15
        short_score += 15

    volatility_ok = cur["atr_pct"] >= settings.atr_min_pct
    if volatility_ok:
        long_score += 10
        short_score += 10

    if cur["close"] > prev["high"]:
        long_score += 15
        reasons_long.append("пробой максимума предыдущей свечи")
    if cur["close"] < prev["low"]:
        short_score += 15
        reasons_short.append("пробой минимума предыдущей свечи")

    threshold = 75
    entry = float(cur["close"])
    atr = float(cur["atr"])

    if long_score >= threshold and long_score > short_score:
        stop = min(float(prev["low"]), entry - 1.3 * atr)
        risk = entry - stop
        tp = entry + risk * settings.reward_risk
        return Signal(symbol, Side.LONG, entry, stop, tp, long_score,
                      "; ".join(reasons_long), candle_time)

    if short_score >= threshold and short_score > long_score:
        stop = max(float(prev["high"]), entry + 1.3 * atr)
        risk = stop - entry
        tp = entry - risk * settings.reward_risk
        return Signal(symbol, Side.SHORT, entry, stop, tp, short_score,
                      "; ".join(reasons_short), candle_time)

    reason = (
        f"нет входа: long_score={long_score}, short_score={short_score}, "
        f"volume_ok={volume_ok}, volatility_ok={volatility_ok}"
    )
    return Signal(symbol, Side.HOLD, None, None, None,
                  max(long_score, short_score), reason, candle_time)
