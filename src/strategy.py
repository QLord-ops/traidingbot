from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

import pandas as pd

from .config import Settings
from .indicators import add_indicators

SCORE_THRESHOLD = 75


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


@dataclass(frozen=True)
class Score:
    long_score: int
    short_score: int
    volume_ok: bool
    volatility_ok: bool
    reasons_long: tuple[str, ...]
    reasons_short: tuple[str, ...]

    def side(self, threshold: int = SCORE_THRESHOLD) -> Side:
        if self.long_score >= threshold and self.long_score > self.short_score:
            return Side.LONG
        if self.short_score >= threshold and self.short_score > self.long_score:
            return Side.SHORT
        return Side.HOLD


def _is_bad(value) -> bool:
    try:
        return value is None or math.isnan(float(value))
    except (TypeError, ValueError):
        return True


def score_candle(cur, prev, trend, settings: Settings) -> Score:
    """Единая score-логика для live-оценки и backtest.

    cur/prev — строки сигнального ТФ с индикаторами; trend — строка старшего ТФ
    (поля close, ema_fast, ema_slow, ema_trend). Любой NaN в обязательных
    индикаторах даёт нулевой скор (HOLD) — никакой торговли на неполных данных.
    """
    required = [
        cur["ema_fast"], cur["ema_slow"], cur["atr"], cur["atr_pct"],
        cur["volume_avg"], trend["close"], trend["ema_fast"],
        trend["ema_slow"], trend["ema_trend"], prev["high"], prev["low"],
    ]
    if any(_is_bad(v) for v in required):
        return Score(0, 0, False, False, ("неполные данные индикаторов",), ())

    long_score = 0
    short_score = 0
    reasons_long: list[str] = []
    reasons_short: list[str] = []

    if trend["close"] > trend["ema_trend"]:
        long_score += 25
        reasons_long.append("1H цена выше EMA тренда")
    elif trend["close"] < trend["ema_trend"]:
        short_score += 25
        reasons_short.append("1H цена ниже EMA тренда")

    if trend["ema_fast"] > trend["ema_slow"]:
        long_score += 20
        reasons_long.append("1H быстрая EMA выше медленной")
    elif trend["ema_fast"] < trend["ema_slow"]:
        short_score += 20
        reasons_short.append("1H быстрая EMA ниже медленной")

    if cur["ema_fast"] > cur["ema_slow"]:
        long_score += 15
        reasons_long.append("импульс сигнального ТФ вверх")
    elif cur["ema_fast"] < cur["ema_slow"]:
        short_score += 15
        reasons_short.append("импульс сигнального ТФ вниз")

    volume_ok = cur["volume"] >= cur["volume_avg"] * settings.volume_multiplier
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

    return Score(long_score, short_score, volume_ok, volatility_ok,
                 tuple(reasons_long), tuple(reasons_short))


def protective_levels(side: Side, entry: float, prev_low: float, prev_high: float,
                      atr: float, reward_risk: float) -> tuple[float, float]:
    """SL по локальной структуре и ATR, TP через Reward/Risk."""
    if side == Side.LONG:
        stop = min(prev_low, entry - 1.3 * atr)
        risk = entry - stop
        tp = entry + risk * reward_risk
    else:
        stop = max(prev_high, entry + 1.3 * atr)
        risk = stop - entry
        tp = entry - risk * reward_risk
    return stop, tp


def evaluate(symbol: str, signal_df: pd.DataFrame, trend_df: pd.DataFrame,
             settings: Settings, last_closed: bool = False) -> Signal:
    """Оценка сигнала по последней закрытой свече.

    last_closed=False (по умолчанию) — данные с live API, где последняя строка
    является формирующейся свечой и отбрасывается. last_closed=True — данные,
    в которых все свечи уже закрыты (кэш, CSV).
    """
    s = add_indicators(
        signal_df, settings.ema_fast, settings.ema_slow,
        settings.ema_trend, settings.atr_period, settings.volume_period
    )
    t = add_indicators(
        trend_df, settings.ema_fast, settings.ema_slow,
        settings.ema_trend, settings.atr_period, settings.volume_period
    )
    offset = 1 if last_closed else 2
    if len(s) < offset + 1 or len(t) < offset:
        return Signal(symbol, Side.HOLD, None, None, None, 0,
                      "недостаточно данных", "")
    cur = s.iloc[-offset]
    prev = s.iloc[-offset - 1]
    trend = t.iloc[-offset]
    candle_time = str(cur["close_time"])

    score = score_candle(cur, prev, trend, settings)
    side = score.side()

    if side == Side.HOLD:
        reason = (
            f"нет входа: long_score={score.long_score}, short_score={score.short_score}, "
            f"volume_ok={score.volume_ok}, volatility_ok={score.volatility_ok}"
        )
        return Signal(symbol, Side.HOLD, None, None, None,
                      max(score.long_score, score.short_score), reason, candle_time)

    entry = float(cur["close"])
    stop, tp = protective_levels(
        side, entry, float(prev["low"]), float(prev["high"]),
        float(cur["atr"]), settings.reward_risk,
    )
    reasons = score.reasons_long if side == Side.LONG else score.reasons_short
    value = score.long_score if side == Side.LONG else score.short_score
    return Signal(symbol, side, entry, stop, tp, value,
                  "; ".join(reasons), candle_time)
