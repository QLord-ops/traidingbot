from __future__ import annotations

from dataclasses import dataclass, asdict
import math
import pandas as pd

from .config import Settings
from .indicators import add_indicators


@dataclass
class Trade:
    side: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    stop: float
    take_profit: float
    pnl_usdt: float
    return_pct: float
    reason: str


@dataclass
class BacktestResult:
    initial_balance: float
    final_balance: float
    net_profit: float
    return_pct: float
    trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    total_fees: float
    total_slippage: float
    trade_log: list[Trade]

    def summary(self) -> dict:
        data = asdict(self)
        data.pop("trade_log")
        return data


def _prepare(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    return add_indicators(
        df, settings.ema_fast, settings.ema_slow, settings.ema_trend,
        settings.atr_period, settings.volume_period,
    )


def run_backtest(
    signal_df: pd.DataFrame,
    trend_df: pd.DataFrame,
    settings: Settings,
    initial_balance: float = 1000.0,
    taker_fee_rate: float = 0.0005,
    slippage_rate: float = 0.0002,
) -> BacktestResult:
    s = _prepare(signal_df.copy(), settings)
    t = _prepare(trend_df.copy(), settings)
    t = t[["close_time", "close", "ema_fast", "ema_slow", "ema_trend"]].rename(
        columns={c: f"trend_{c}" for c in ["close", "ema_fast", "ema_slow", "ema_trend"]}
    )
    s = pd.merge_asof(
        s.sort_values("close_time"),
        t.sort_values("close_time"),
        on="close_time",
        direction="backward",
    )

    balance = initial_balance
    peak = initial_balance
    max_dd = 0.0
    fees_total = 0.0
    slippage_total = 0.0
    trades: list[Trade] = []
    position = None

    start = max(settings.ema_trend + 2, settings.volume_period + 2)
    for i in range(start, len(s) - 1):
        cur = s.iloc[i]
        prev = s.iloc[i - 1]

        if position:
            side = position["side"]
            exit_price = None
            reason = None
            # Conservative assumption: if stop and target occur in same candle, stop is counted first.
            if side == "LONG":
                if cur["low"] <= position["stop"]:
                    exit_price, reason = position["stop"], "STOP"
                elif cur["high"] >= position["tp"]:
                    exit_price, reason = position["tp"], "TAKE_PROFIT"
            else:
                if cur["high"] >= position["stop"]:
                    exit_price, reason = position["stop"], "STOP"
                elif cur["low"] <= position["tp"]:
                    exit_price, reason = position["tp"], "TAKE_PROFIT"

            if exit_price is not None:
                signed_move = (exit_price - position["entry"]) * (1 if side == "LONG" else -1)
                gross = signed_move * position["qty"]
                exit_notional = exit_price * position["qty"]
                exit_fee = exit_notional * taker_fee_rate
                exit_slip = exit_notional * slippage_rate
                pnl = gross - position["entry_fee"] - exit_fee - position["entry_slip"] - exit_slip
                balance += pnl
                fees_total += position["entry_fee"] + exit_fee
                slippage_total += position["entry_slip"] + exit_slip
                trades.append(Trade(
                    side=side,
                    entry_time=str(position["entry_time"]),
                    exit_time=str(cur["close_time"]),
                    entry=position["entry"],
                    exit=exit_price,
                    stop=position["stop"],
                    take_profit=position["tp"],
                    pnl_usdt=pnl,
                    return_pct=pnl / max(position["margin"], 1e-9) * 100,
                    reason=reason,
                ))
                peak = max(peak, balance)
                max_dd = max(max_dd, (peak - balance) / peak if peak else 0)
                position = None
                continue

        if position or pd.isna(cur["trend_ema_trend"]) or pd.isna(cur["volume_avg"]):
            continue

        volume_ok = cur["volume"] >= cur["volume_avg"] * settings.volume_multiplier
        volatility_ok = cur["atr_pct"] >= settings.atr_min_pct
        long_score = 0
        short_score = 0
        if cur["trend_close"] > cur["trend_ema_trend"]: long_score += 25
        if cur["trend_close"] < cur["trend_ema_trend"]: short_score += 25
        if cur["trend_ema_fast"] > cur["trend_ema_slow"]: long_score += 20
        if cur["trend_ema_fast"] < cur["trend_ema_slow"]: short_score += 20
        if cur["ema_fast"] > cur["ema_slow"]: long_score += 15
        if cur["ema_fast"] < cur["ema_slow"]: short_score += 15
        if volume_ok: long_score += 15; short_score += 15
        if volatility_ok: long_score += 10; short_score += 10
        if cur["close"] > prev["high"]: long_score += 15
        if cur["close"] < prev["low"]: short_score += 15

        side = None
        if long_score >= 75 and long_score > short_score: side = "LONG"
        elif short_score >= 75 and short_score > long_score: side = "SHORT"
        if side is None:
            continue

        raw_entry = float(s.iloc[i + 1]["open"])
        entry = raw_entry * (1 + slippage_rate if side == "LONG" else 1 - slippage_rate)
        atr = float(cur["atr"])
        if side == "LONG":
            stop = min(float(prev["low"]), entry - 1.3 * atr)
            risk_per_unit = entry - stop
            tp = entry + risk_per_unit * settings.reward_risk
        else:
            stop = max(float(prev["high"]), entry + 1.3 * atr)
            risk_per_unit = stop - entry
            tp = entry - risk_per_unit * settings.reward_risk
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
        entry_fee = notional * taker_fee_rate
        entry_slip = notional * slippage_rate
        position = {
            "side": side, "entry": entry, "stop": stop, "tp": tp, "qty": qty,
            "entry_time": s.iloc[i + 1]["open_time"], "entry_fee": entry_fee,
            "entry_slip": entry_slip, "margin": margin,
        }

    wins = sum(t.pnl_usdt > 0 for t in trades)
    losses = sum(t.pnl_usdt <= 0 for t in trades)
    gross_profit = sum(max(t.pnl_usdt, 0) for t in trades)
    gross_loss = abs(sum(min(t.pnl_usdt, 0) for t in trades))
    pf = gross_profit / gross_loss if gross_loss else math.inf if gross_profit else 0.0
    return BacktestResult(
        initial_balance=initial_balance,
        final_balance=balance,
        net_profit=balance - initial_balance,
        return_pct=(balance / initial_balance - 1) * 100,
        trades=len(trades), wins=wins, losses=losses,
        win_rate=(wins / len(trades) * 100) if trades else 0.0,
        profit_factor=pf, max_drawdown_pct=max_dd * 100,
        total_fees=fees_total, total_slippage=slippage_total,
        trade_log=trades,
    )
