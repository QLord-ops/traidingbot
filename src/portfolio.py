from __future__ import annotations

"""Портфельный backtest: одна позиция на весь счёт, много символов.

Каждую свечу оцениваются ВСЕ символы; при нескольких одновременных сигналах
выбирается лучший по ранжировщику. Это проверяемая версия «анализирует все
монеты и выбирает лучший вход»: правило выбора фиксировано и прогоняется
по истории точно так же, как сама стратегия.

Ранжировщики (rank_mode):
- "trend"    — сила тренда старшего ТФ: |trend_close/EMA200 − 1|
               (обоснование: time-series momentum — более сильный тренд
               имеет большее ожидание продолжения);
- "breakout" — величина пробоя канала в ATR;
- "none"     — базовая линия: первый по алфавиту (как вёл бы себя движок
               без ранжирования).
"""

import pandas as pd

from . import macro_calendar
from .backtest import (BacktestParams, BacktestResult, Trade, _build_result,
                       _FundingBook, _merge_trend, _prepare)
from .config import Settings
from .strategy import Side, donchian_side

TREND_COLS = {"close": "trend_close", "ema_fast": "trend_ema_fast",
              "ema_slow": "trend_ema_slow", "ema_trend": "trend_ema_trend"}


def signal_strength(cur, trend_row, side: Side, rank_mode: str) -> float:
    if rank_mode == "breakout":
        atr = float(cur["atr"]) or 1e-9
        if side == Side.LONG:
            return (float(cur["close"]) - float(cur["donchian_high"])) / atr
        return (float(cur["donchian_low"]) - float(cur["close"])) / atr
    if rank_mode == "trend":
        return abs(float(trend_row["close"]) / float(trend_row["ema_trend"]) - 1)
    return 0.0


def run_portfolio_backtest(
    data: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame | None]],
    settings: Settings,
    params: BacktestParams | None = None,
    rank_mode: str = "trend",
) -> BacktestResult:
    """data: symbol -> (signal_df, trend_df, funding_df | None)."""
    if settings.strategy_mode != "donchian":
        raise ValueError("Портфельный backtest реализован для donchian")
    p = params or BacktestParams()

    frames: dict[str, pd.DataFrame] = {}
    row_at: dict[str, dict] = {}
    funding: dict[str, _FundingBook] = {}
    for symbol in sorted(data):
        sdf, tdf, fdf = data[symbol]
        s = _prepare(sdf.copy(), settings)
        s["donchian_high"] = s["high"].rolling(settings.donchian_period).max().shift(1)
        s["donchian_low"] = s["low"].rolling(settings.donchian_period).min().shift(1)
        s = _merge_trend(s, _prepare(tdf.copy(), settings)).reset_index(drop=True)
        frames[symbol] = s
        row_at[symbol] = {ts: i for i, ts in enumerate(s["close_time"])}
        funding[symbol] = _FundingBook(fdf if p.apply_funding else None)

    warmup = max(settings.ema_trend, settings.donchian_period,
                 settings.atr_period) + 2
    timeline = sorted(set().union(*[set(f["close_time"]) for f in frames.values()]))

    balance = p.initial_balance
    fees_total = slippage_total = funding_total = 0.0
    trades: list[Trade] = []
    position: dict | None = None
    equity_times: list[str] = []
    equity_values: list[float] = []
    day = None
    day_start_balance = balance
    trades_today = 0
    day_locked = False
    skipped_by_daily_loss = skipped_by_trade_limit = skipped_by_macro = 0

    for ts in timeline:
        ts_day = ts.date()
        if ts_day != day:
            day = ts_day
            day_start_balance = balance
            trades_today = 0
            day_locked = False

        # --- сопровождение позиции -----------------------------------------
        if position and ts in row_at[position["symbol"]]:
            f = frames[position["symbol"]]
            cur = f.iloc[row_at[position["symbol"]][ts]]
            side = position["side"]
            sign = 1 if side == "LONG" else -1
            exit_price = None
            reason = None
            if sign * (float(cur["open"]) - position["stop"]) <= 0:
                exit_price, reason = float(cur["open"]), "STOP_GAP"
            elif (side == "LONG" and cur["low"] <= position["stop"]) or \
                    (side == "SHORT" and cur["high"] >= position["stop"]):
                exit_price, reason = position["stop"], "STOP"

            fcost = funding[position["symbol"]].cost(
                position["last_funding_check"], cur["close_time"],
                position["qty"], float(cur["close"]), side)
            position["funding"] += fcost
            position["last_funding_check"] = cur["close_time"]

            if exit_price is not None:
                fill = exit_price * (1 - sign * p.slippage_rate)
                slip_cost = abs(fill - exit_price) * position["qty"]
                exit_fee = fill * position["qty"] * p.taker_fee_rate
                gross = (fill - position["entry"]) * sign * position["qty"]
                fees = position["entry_fee"] + exit_fee
                pnl = gross - fees - position["funding"]
                balance += pnl
                fees_total += fees
                slippage_total += position["entry_slip"] + slip_cost
                funding_total += position["funding"]
                trades.append(Trade(
                    side=side, entry_time=str(position["entry_time"]),
                    exit_time=str(cur["close_time"]),
                    entry=position["entry"], exit=fill,
                    stop=position["stop"], take_profit=None,
                    qty=position["qty"], pnl_usdt=pnl,
                    r_multiple=pnl / max(position["risk_usdt"], 1e-9),
                    fees_usdt=fees, funding_usdt=position["funding"],
                    return_pct=pnl / max(position["margin"], 1e-9) * 100,
                    reason=f"{reason} [{position['symbol']}]",
                ))
                position = None
                if balance <= day_start_balance * (1 - settings.daily_loss_limit):
                    day_locked = True
            elif not pd.isna(cur["atr"]):
                k = settings.trail_atr_mult
                new_stop = float(cur["close"]) - sign * k * float(cur["atr"])
                if sign * (new_stop - position["stop"]) > 0:
                    position["stop"] = new_stop

        unrealized = 0.0
        if position and ts in row_at[position["symbol"]]:
            cur_close = float(frames[position["symbol"]]
                              .iloc[row_at[position["symbol"]][ts]]["close"])
            sign = 1 if position["side"] == "LONG" else -1
            unrealized = (cur_close - position["entry"]) * sign * position["qty"]
        equity_times.append(str(ts))
        equity_values.append(balance + unrealized)

        # --- вход: лучший сигнал из всех символов ---------------------------
        if position is not None:
            continue
        if day_locked:
            skipped_by_daily_loss += 1
            continue
        if trades_today >= settings.max_trades_per_day:
            skipped_by_trade_limit += 1
            continue

        candidates = []
        for symbol, f in frames.items():
            i = row_at[symbol].get(ts)
            if i is None or i < warmup or i + 1 >= len(f):
                continue
            cur = f.iloc[i]
            trend_row = {k: cur[v] for k, v in TREND_COLS.items()}
            side = donchian_side(cur, trend_row, settings)
            if side == Side.HOLD:
                continue
            entry_time = f.iloc[i + 1]["open_time"]
            if settings.macro_filter and macro_calendar.in_blackout(
                    entry_time, settings.macro_block_before_h,
                    settings.macro_block_after_h):
                skipped_by_macro += 1
                continue
            candidates.append((
                signal_strength(cur, trend_row, side, rank_mode),
                symbol, side, i,
            ))
        if not candidates:
            continue
        strength, symbol, side, i = max(candidates, key=lambda c: (c[0], c[1]))
        f = frames[symbol]
        cur = f.iloc[i]
        raw_entry = float(f.iloc[i + 1]["open"])
        sign = 1 if side == Side.LONG else -1
        entry = raw_entry * (1 + sign * p.slippage_rate)
        stop = entry - sign * settings.trail_atr_mult * float(cur["atr"])
        risk_per_unit = (entry - stop) * sign
        if risk_per_unit <= 0:
            continue
        risk_budget = balance * settings.risk_per_trade
        qty = risk_budget / risk_per_unit
        notional = qty * entry
        margin = notional / settings.leverage
        if margin > balance:
            qty *= balance / margin
            notional = qty * entry
            margin = balance
        if qty <= 0 or balance <= 0:
            continue
        position = {
            "symbol": symbol, "side": side.value, "entry": entry, "stop": stop,
            "qty": qty, "entry_time": f.iloc[i + 1]["open_time"],
            "entry_fee": notional * p.taker_fee_rate,
            "entry_slip": abs(entry - raw_entry) * qty, "margin": margin,
            "risk_usdt": risk_budget, "funding": 0.0,
            "last_funding_check": f.iloc[i + 1]["open_time"],
        }
        trades_today += 1

    return _build_result(p, balance, trades, fees_total, slippage_total,
                         funding_total, equity_times, equity_values,
                         skipped_by_daily_loss, skipped_by_trade_limit,
                         skipped_by_macro)
