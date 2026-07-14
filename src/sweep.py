from __future__ import annotations

"""Сетка backtest-прогонов: подбор более частого варианта donchian-стратегии.

Каждая конфигурация (ТФ, период канала, трейлинг) прогоняется на нескольких
символах за N дней с реальными издержками. Для оценки устойчивости сделки
одного прогона делятся на 6 равных временных окон (без переоптимизации это
эквивалент walk-forward и в 7 раз дешевле). Риск фиксируется маленьким
(0.25%), чтобы компаундинг не искажал сравнение; месячная доходность при
другом риске масштабируется: %/мес ~= сделок/мес x avg_R x риск.
"""

import argparse
import dataclasses
import json
from pathlib import Path

import pandas as pd

from .backtest import BacktestParams, BacktestResult, run_backtest
from .binance_client import BinanceFuturesClient
from .config import Settings
from .data import get_funding_rates, get_klines

GRID = [
    # (signal_tf, trend_tf, donchian_period, trail_atr_mult)
    ("15m", "4h", 48, 2.0), ("15m", "4h", 48, 3.0),
    ("15m", "4h", 96, 2.0), ("15m", "4h", 96, 3.0),
    ("15m", "4h", 192, 2.0), ("15m", "4h", 192, 3.0),
    ("1h", "1d", 24, 2.0), ("1h", "1d", 24, 3.0),
    ("1h", "1d", 48, 2.0), ("1h", "1d", 48, 3.0),
    ("1h", "1d", 96, 2.0), ("1h", "1d", 96, 3.0),
    ("4h", "1d", 48, 3.0),  # текущий базовый вариант для сравнения
]


def wf_positive_windows(result: BacktestResult, n_windows: int = 6) -> int:
    """Число временных окон с неотрицательным суммарным PnL сделок.

    Пустое окно считается нейтральным (PnL 0 — не хуже нуля).
    """
    if not result.trade_log:
        return 0
    times = pd.Series(pd.to_datetime([t.exit_time for t in result.trade_log]))
    pnls = pd.Series([t.pnl_usdt for t in result.trade_log])
    if times.nunique() == 1:
        return 1 if pnls.sum() >= 0 else 0
    bins = pd.cut(times, bins=n_windows)
    window_pnl = pnls.groupby(bins, observed=False).sum().fillna(0.0)
    return int((window_pnl >= 0).sum())


def main() -> None:
    parser = argparse.ArgumentParser(description="Сетка donchian-вариантов")
    parser.add_argument("--days", type=int, default=1095)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT")
    args = parser.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    client = BinanceFuturesClient(testnet=False)
    end = pd.Timestamp.now(tz="UTC")
    start = end - pd.Timedelta(days=args.days)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    base = dataclasses.replace(
        Settings(),
        strategy_mode="donchian", trading_mode="testnet",
        risk_per_trade=0.0025, daily_loss_limit=0.20, max_trades_per_day=15,
    )
    params = BacktestParams()  # реалистичные издержки по умолчанию

    frames: dict[tuple[str, str], pd.DataFrame] = {}
    funding: dict[str, pd.DataFrame] = {}

    def data_for(symbol: str, interval: str) -> pd.DataFrame:
        key = (symbol, interval)
        if key not in frames:
            print(f"загрузка {symbol} {interval}...", flush=True)
            frames[key] = get_klines(client, symbol, interval, start_ms, end_ms)
        return frames[key]

    rows = []
    months = args.days / 30.44
    for signal_tf, trend_tf, period, trail in GRID:
        for symbol in symbols:
            signal_df = data_for(symbol, signal_tf)
            trend_df = data_for(symbol, trend_tf)
            if symbol not in funding:
                funding[symbol] = get_funding_rates(client, symbol, start_ms, end_ms)
            settings = dataclasses.replace(
                base, signal_interval=signal_tf, trend_interval=trend_tf,
                donchian_period=period, trail_atr_mult=trail,
            )
            settings.validate()
            result = run_backtest(signal_df, trend_df, settings, params,
                                  funding_df=funding[symbol])
            rows.append({
                "signal_tf": signal_tf, "trend_tf": trend_tf,
                "period": period, "trail": trail, "symbol": symbol,
                "trades": result.trades,
                "trades_per_month": round(result.trades / months, 2),
                "win_rate": round(result.win_rate, 1),
                "profit_factor": (round(result.profit_factor, 3)
                                  if result.profit_factor != float("inf") else 99.0),
                "avg_r": round(result.avg_r, 4),
                "return_pct": round(result.return_pct, 2),
                "max_dd_pct": round(result.max_drawdown_pct, 2),
                "wf_pos_of_6": wf_positive_windows(result),
            })
            r = rows[-1]
            print(f"{signal_tf}/{trend_tf} P{period} T{trail} {symbol}: "
                  f"trades={r['trades']} avgR={r['avg_r']:+.3f} PF={r['profit_factor']} "
                  f"WF+{r['wf_pos_of_6']}/6", flush=True)

    df = pd.DataFrame(rows)
    out = Path("data/sweep_results.csv")
    df.to_csv(out, index=False)

    # агрегат по конфигурациям: суммарные сделки, средневзвешенный R
    agg_rows = []
    for (stf, ttf, period, trail), g in df.groupby(
            ["signal_tf", "trend_tf", "period", "trail"]):
        total_trades = int(g["trades"].sum())
        weighted_r = (
            float((g["avg_r"] * g["trades"]).sum() / total_trades)
            if total_trades else 0.0
        )
        agg_rows.append({
            "config": f"{stf}/{ttf} P{period} T{trail}",
            "trades_3y": total_trades,
            "trades_per_month": round(total_trades / months, 1),
            "avg_r": round(weighted_r, 4),
            "expected_R_per_month": round(total_trades / months * weighted_r, 3),
            "min_wf_pos": int(g["wf_pos_of_6"].min()),
            "symbols_positive": int((g["avg_r"] > 0).sum()),
        })
    agg = pd.DataFrame(agg_rows).sort_values("expected_R_per_month", ascending=False)
    agg.to_csv("data/sweep_aggregate.csv", index=False)
    print("\n=== Агрегат по конфигурациям (сортировка по ожидаемым R в месяц) ===")
    print(agg.to_string(index=False))
    print(json.dumps({"best": agg.iloc[0].to_dict()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
