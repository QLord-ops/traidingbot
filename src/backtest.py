from __future__ import annotations

from dataclasses import dataclass, field, asdict
import math

import pandas as pd

from .config import Settings
from .indicators import add_indicators
from .strategy import Side, score_candle, protective_levels


@dataclass
class Trade:
    side: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    stop: float
    take_profit: float
    qty: float
    pnl_usdt: float
    r_multiple: float
    fees_usdt: float
    funding_usdt: float
    return_pct: float
    reason: str


@dataclass(frozen=True)
class BacktestParams:
    initial_balance: float = 1000.0
    taker_fee_rate: float = 0.0005   # вход и SL — market
    maker_fee_rate: float = 0.0002   # TP — limit
    slippage_rate: float = 0.0002    # только для market-исполнений
    apply_funding: bool = True


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
    total_funding: float
    expectancy_usdt: float
    avg_r: float
    max_consecutive_losses: int
    max_consecutive_wins: int
    skipped_by_daily_loss: int
    skipped_by_trade_limit: int
    monthly_returns: dict[str, float] = field(default_factory=dict)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
    drawdown_curve: list[tuple[str, float]] = field(default_factory=list)
    trade_log: list[Trade] = field(default_factory=list)

    def summary(self) -> dict:
        data = asdict(self)
        data.pop("trade_log")
        data.pop("equity_curve")
        data.pop("drawdown_curve")
        return data


def _prepare(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    return add_indicators(
        df, settings.ema_fast, settings.ema_slow, settings.ema_trend,
        settings.atr_period, settings.volume_period,
    )


def _merge_trend(s: pd.DataFrame, t: pd.DataFrame) -> pd.DataFrame:
    """Присоединяет к сигнальному ТФ последнюю ЗАКРЫТУЮ свечу тренда (без look-ahead)."""
    t = t[["close_time", "close", "ema_fast", "ema_slow", "ema_trend"]].rename(
        columns={c: f"trend_{c}" for c in ["close", "ema_fast", "ema_slow", "ema_trend"]}
    )
    return pd.merge_asof(
        s.sort_values("close_time"),
        t.sort_values("close_time"),
        on="close_time",
        direction="backward",
    )


def _to_utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")


class _FundingBook:
    """Funding-события, сгруппированные по свече сигнального ТФ."""

    def __init__(self, funding_df: pd.DataFrame | None):
        if funding_df is None or funding_df.empty:
            self.times = None
            self.rates = None
        else:
            self.times = pd.DatetimeIndex(
                pd.to_datetime(funding_df["funding_time"], utc=True)
            )
            self.rates = funding_df["funding_rate"].to_numpy()

    def cost(self, open_time, close_time, qty: float, price: float, side: str) -> float:
        """Положительное значение = позиция платит funding."""
        if self.times is None:
            return 0.0
        mask = (self.times > _to_utc(open_time)) & (self.times <= _to_utc(close_time))
        if not mask.any():
            return 0.0
        total_rate = float(self.rates[mask].sum())
        sign = 1.0 if side == "LONG" else -1.0
        return total_rate * qty * price * sign


def run_backtest(
    signal_df: pd.DataFrame,
    trend_df: pd.DataFrame,
    settings: Settings,
    params: BacktestParams | None = None,
    funding_df: pd.DataFrame | None = None,
    trade_start: pd.Timestamp | None = None,
    trade_end: pd.Timestamp | None = None,
) -> BacktestResult:
    """Backtest стратегии на закрытых свечах.

    Гарантии против look-ahead: сигнал считается по закрытой свече i,
    вход — по open свечи i+1; тренд мержится только назад по времени.
    Консервативные допущения: в свече, где задеты и SL и TP, засчитывается SL;
    гэп сквозь SL исполняется по open свечи (хуже стопа).

    trade_start/trade_end ограничивают ОКНО ВХОДОВ (индикаторы считаются по
    всем данным) — используется для out-of-sample и walk-forward.
    """
    p = params or BacktestParams()
    s = _prepare(signal_df.copy(), settings)
    t = _prepare(trend_df.copy(), settings)
    s = _merge_trend(s, t)
    trend_cols = {"close": "trend_close", "ema_fast": "trend_ema_fast",
                  "ema_slow": "trend_ema_slow", "ema_trend": "trend_ema_trend"}
    funding = _FundingBook(funding_df if p.apply_funding else None)

    balance = p.initial_balance
    fees_total = 0.0
    slippage_total = 0.0
    funding_total = 0.0
    trades: list[Trade] = []
    position: dict | None = None
    equity_times: list[str] = []
    equity_values: list[float] = []

    day = None
    day_start_balance = balance
    trades_today = 0
    day_locked = False
    skipped_by_daily_loss = 0
    skipped_by_trade_limit = 0

    start = max(settings.ema_trend, settings.volume_period, settings.atr_period) + 2
    for i in range(start, len(s) - 1):
        cur = s.iloc[i]
        prev = s.iloc[i - 1]

        candle_day = cur["close_time"].date()
        if candle_day != day:
            day = candle_day
            day_start_balance = balance
            trades_today = 0
            day_locked = False

        # --- сопровождение открытой позиции --------------------------------
        if position:
            side = position["side"]
            exit_price = None
            exit_is_market = True
            reason = None
            if side == "LONG":
                if cur["open"] <= position["stop"]:
                    exit_price, reason = float(cur["open"]), "STOP_GAP"
                elif cur["low"] <= position["stop"]:
                    exit_price, reason = position["stop"], "STOP"
                elif cur["high"] >= position["tp"]:
                    exit_price, reason, exit_is_market = position["tp"], "TAKE_PROFIT", False
            else:
                if cur["open"] >= position["stop"]:
                    exit_price, reason = float(cur["open"]), "STOP_GAP"
                elif cur["high"] >= position["stop"]:
                    exit_price, reason = position["stop"], "STOP"
                elif cur["low"] <= position["tp"]:
                    exit_price, reason, exit_is_market = position["tp"], "TAKE_PROFIT", False

            fcost = funding.cost(position["last_funding_check"], cur["close_time"],
                                 position["qty"], float(cur["close"]), side)
            position["funding"] += fcost
            position["last_funding_check"] = cur["close_time"]

            if exit_price is not None:
                sign = 1 if side == "LONG" else -1
                if exit_is_market:
                    fill = exit_price * (1 - sign * p.slippage_rate)
                    slip_cost = abs(fill - exit_price) * position["qty"]
                    exit_fee = fill * position["qty"] * p.taker_fee_rate
                else:
                    fill = exit_price
                    slip_cost = 0.0
                    exit_fee = fill * position["qty"] * p.maker_fee_rate
                gross = (fill - position["entry"]) * sign * position["qty"]
                fees = position["entry_fee"] + exit_fee
                pnl = gross - fees - position["funding"]
                balance += pnl
                fees_total += fees
                slippage_total += position["entry_slip"] + slip_cost
                funding_total += position["funding"]
                trades.append(Trade(
                    side=side,
                    entry_time=str(position["entry_time"]),
                    exit_time=str(cur["close_time"]),
                    entry=position["entry"], exit=fill,
                    stop=position["stop"], take_profit=position["tp"],
                    qty=position["qty"], pnl_usdt=pnl,
                    r_multiple=pnl / max(position["risk_usdt"], 1e-9),
                    fees_usdt=fees, funding_usdt=position["funding"],
                    return_pct=pnl / max(position["margin"], 1e-9) * 100,
                    reason=reason,
                ))
                position = None
                if balance <= day_start_balance * (1 - settings.daily_loss_limit):
                    day_locked = True

        # --- equity (баланс + нереализованный PnL) --------------------------
        unrealized = 0.0
        if position:
            sign = 1 if position["side"] == "LONG" else -1
            unrealized = (float(cur["close"]) - position["entry"]) * sign * position["qty"]
        equity_times.append(str(cur["close_time"]))
        equity_values.append(balance + unrealized)

        # --- новый вход ------------------------------------------------------
        if position is not None:
            continue
        if trade_start is not None and cur["close_time"] < trade_start:
            continue
        if trade_end is not None and cur["close_time"] >= trade_end:
            continue
        if day_locked:
            skipped_by_daily_loss += 1
            continue
        if trades_today >= settings.max_trades_per_day:
            skipped_by_trade_limit += 1
            continue

        trend_row = {k: cur[v] for k, v in trend_cols.items()}
        score = score_candle(cur, prev, trend_row, settings)
        side = score.side()
        if side == Side.HOLD:
            continue

        raw_entry = float(s.iloc[i + 1]["open"])
        sign = 1 if side == Side.LONG else -1
        entry = raw_entry * (1 + sign * p.slippage_rate)
        entry_slip = abs(entry - raw_entry)
        stop, tp = protective_levels(
            side, entry, float(prev["low"]), float(prev["high"]),
            float(cur["atr"]), settings.reward_risk,
        )
        risk_per_unit = (entry - stop) * sign
        if risk_per_unit <= 0:
            continue

        risk_budget = balance * settings.risk_per_trade
        qty = risk_budget / risk_per_unit
        notional = qty * entry
        margin = notional / settings.leverage
        if margin > balance:  # ограничение доступной маржой (плечо не превышается)
            qty *= balance / margin
            notional = qty * entry
            margin = balance
        if qty <= 0 or balance <= 0:
            continue
        entry_fee = notional * p.taker_fee_rate
        position = {
            "side": side.value, "entry": entry, "stop": stop, "tp": tp, "qty": qty,
            "entry_time": s.iloc[i + 1]["open_time"], "entry_fee": entry_fee,
            "entry_slip": entry_slip * qty, "margin": margin,
            "risk_usdt": risk_budget, "funding": 0.0,
            "last_funding_check": s.iloc[i + 1]["open_time"],
        }
        trades_today += 1

    return _build_result(p, balance, trades, fees_total, slippage_total, funding_total,
                         equity_times, equity_values,
                         skipped_by_daily_loss, skipped_by_trade_limit)


def _build_result(p: BacktestParams, balance: float, trades: list[Trade],
                  fees_total: float, slippage_total: float, funding_total: float,
                  equity_times: list[str], equity_values: list[float],
                  skipped_by_daily_loss: int, skipped_by_trade_limit: int) -> BacktestResult:
    wins = sum(t.pnl_usdt > 0 for t in trades)
    losses = sum(t.pnl_usdt <= 0 for t in trades)
    gross_profit = sum(max(t.pnl_usdt, 0) for t in trades)
    gross_loss = abs(sum(min(t.pnl_usdt, 0) for t in trades))
    pf = gross_profit / gross_loss if gross_loss else (math.inf if gross_profit else 0.0)

    max_dd = 0.0
    peak = -math.inf
    drawdown_curve: list[tuple[str, float]] = []
    for ts, eq in zip(equity_times, equity_values):
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        drawdown_curve.append((ts, dd))

    monthly: dict[str, float] = {}
    if equity_times:
        eq = pd.Series(equity_values, index=pd.to_datetime(equity_times))
        by_month = eq.resample("ME").last()
        prev_val = p.initial_balance
        for ts, val in by_month.items():
            if pd.isna(val):
                continue
            monthly[ts.strftime("%Y-%m")] = (val / prev_val - 1) * 100
            prev_val = val

    max_consec_losses = max_consec_wins = cl = cw = 0
    for t in trades:
        if t.pnl_usdt <= 0:
            cl += 1
            cw = 0
        else:
            cw += 1
            cl = 0
        max_consec_losses = max(max_consec_losses, cl)
        max_consec_wins = max(max_consec_wins, cw)

    n = len(trades)
    return BacktestResult(
        initial_balance=p.initial_balance,
        final_balance=balance,
        net_profit=balance - p.initial_balance,
        return_pct=(balance / p.initial_balance - 1) * 100,
        trades=n, wins=wins, losses=losses,
        win_rate=(wins / n * 100) if n else 0.0,
        profit_factor=pf, max_drawdown_pct=max_dd,
        total_fees=fees_total, total_slippage=slippage_total,
        total_funding=funding_total,
        expectancy_usdt=(sum(t.pnl_usdt for t in trades) / n) if n else 0.0,
        avg_r=(sum(t.r_multiple for t in trades) / n) if n else 0.0,
        max_consecutive_losses=max_consec_losses,
        max_consecutive_wins=max_consec_wins,
        skipped_by_daily_loss=skipped_by_daily_loss,
        skipped_by_trade_limit=skipped_by_trade_limit,
        monthly_returns=monthly,
        equity_curve=list(zip(equity_times, equity_values)),
        drawdown_curve=drawdown_curve,
        trade_log=trades,
    )


@dataclass
class WalkForwardWindow:
    label: str
    start: str
    end: str
    result: BacktestResult


def walk_forward(
    signal_df: pd.DataFrame,
    trend_df: pd.DataFrame,
    settings: Settings,
    params: BacktestParams | None = None,
    funding_df: pd.DataFrame | None = None,
    n_windows: int = 4,
) -> list[WalkForwardWindow]:
    """Делит диапазон на последовательные окна и тестирует каждое отдельно.

    Индикаторы считаются по всей истории (без утечки — merge только назад),
    входы ограничены окном. Последнее окно — out-of-sample по отношению к
    любой подстройке параметров на первых окнах.
    """
    if signal_df.empty or n_windows < 2:
        raise ValueError("Нужно >= 2 окон и непустые данные")
    t0 = signal_df["close_time"].iloc[0]
    t1 = signal_df["close_time"].iloc[-1]
    edges = pd.date_range(t0, t1, periods=n_windows + 1)
    windows: list[WalkForwardWindow] = []
    for k in range(n_windows):
        w_start, w_end = edges[k], edges[k + 1]
        res = run_backtest(
            signal_df, trend_df, settings, params, funding_df,
            trade_start=w_start, trade_end=w_end,
        )
        windows.append(WalkForwardWindow(
            label=f"Окно {k + 1}/{n_windows}",
            start=str(w_start), end=str(w_end), result=res,
        ))
    return windows
