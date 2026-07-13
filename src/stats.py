from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class TradeStats:
    """Агрегированная статистика закрытых Testnet-сделок."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    expectancy: float = 0.0
    avg_r: float = 0.0
    profit_factor: float = 0.0
    max_consecutive_losses: int = 0
    by_symbol: dict[str, dict] = field(default_factory=dict)


def _r_multiple(trade: dict) -> float | None:
    """R сделки из журнальных полей: pnl / (qty · |entry − stop|)."""
    pnl = trade.get("realized_pnl")
    qty = trade.get("qty") or 0
    entry = trade.get("entry")
    stop = trade.get("stop")
    if pnl is None or entry is None or stop is None or qty <= 0:
        return None
    risk = qty * abs(entry - stop)
    if risk <= 0:
        return None
    return pnl / risk


def compute_trade_stats(trades: list[dict]) -> TradeStats:
    """Считает статистику по закрытым сделкам журнала (realized_pnl задан)."""
    closed = [t for t in trades if t.get("realized_pnl") is not None]
    stats = TradeStats(trades=len(closed))
    if not closed:
        return stats

    gross_profit = 0.0
    gross_loss = 0.0
    r_values: list[float] = []
    consec = 0
    for t in closed:
        pnl = float(t["realized_pnl"])
        stats.total_pnl += pnl
        if pnl > 0:
            stats.wins += 1
            gross_profit += pnl
            consec = 0
        else:
            stats.losses += 1
            gross_loss += -pnl
            consec += 1
        stats.max_consecutive_losses = max(stats.max_consecutive_losses, consec)
        r = _r_multiple(t)
        if r is not None:
            r_values.append(r)

        symbol = t.get("symbol", "?")
        agg = stats.by_symbol.setdefault(
            symbol, {"trades": 0, "wins": 0, "pnl": 0.0}
        )
        agg["trades"] += 1
        agg["wins"] += 1 if pnl > 0 else 0
        agg["pnl"] += pnl

    stats.win_rate = stats.wins / stats.trades * 100
    stats.expectancy = stats.total_pnl / stats.trades
    stats.avg_r = sum(r_values) / len(r_values) if r_values else 0.0
    stats.profit_factor = (
        gross_profit / gross_loss if gross_loss > 0
        else (math.inf if gross_profit > 0 else 0.0)
    )
    return stats
