from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

import pandas as pd

from .backtest import BacktestParams, run_backtest, walk_forward
from .binance_client import BinanceFuturesClient
from .config import Settings
from .data import get_klines, get_funding_rates


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest стратегии по публичным данным")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--days", type=int, default=180, help="Глубина истории в днях")
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--taker-fee", type=float, default=0.0005)
    parser.add_argument("--maker-fee", type=float, default=0.0002)
    parser.add_argument("--slippage", type=float, default=0.0002)
    parser.add_argument("--no-funding", action="store_true")
    parser.add_argument("--walk-forward", type=int, default=0, metavar="N",
                        help="Разбить период на N окон")
    parser.add_argument("--signal-csv")
    parser.add_argument("--trend-csv")
    parser.add_argument("--strategy", choices=["score", "donchian"], default=None,
                        help="Переопределить STRATEGY_MODE")
    parser.add_argument("--signal-interval", default=None, help="напр. 4h")
    parser.add_argument("--trend-interval", default=None, help="напр. 1d")
    parser.add_argument("--donchian-period", type=int, default=None)
    parser.add_argument("--trail-atr", type=float, default=None)
    args = parser.parse_args()

    settings = Settings()
    overrides = {}
    if args.strategy:
        overrides["strategy_mode"] = args.strategy
    if args.signal_interval:
        overrides["signal_interval"] = args.signal_interval
    if args.trend_interval:
        overrides["trend_interval"] = args.trend_interval
    if args.donchian_period:
        overrides["donchian_period"] = args.donchian_period
    if args.trail_atr:
        overrides["trail_atr_mult"] = args.trail_atr
    if overrides:
        settings = dataclasses.replace(settings, **overrides)
    settings.validate()
    params = BacktestParams(
        initial_balance=args.balance,
        taker_fee_rate=args.taker_fee,
        maker_fee_rate=args.maker_fee,
        slippage_rate=args.slippage,
        apply_funding=not args.no_funding,
    )

    funding_df = None
    if args.signal_csv and args.trend_csv:
        signal_df = pd.read_csv(args.signal_csv, parse_dates=["open_time", "close_time"])
        trend_df = pd.read_csv(args.trend_csv, parse_dates=["open_time", "close_time"])
    else:
        client = BinanceFuturesClient(testnet=False)
        end = pd.Timestamp.now(tz="UTC")
        start = end - pd.Timedelta(days=args.days)
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        signal_df = get_klines(client, args.symbol, settings.signal_interval, start_ms, end_ms)
        trend_df = get_klines(client, args.symbol, settings.trend_interval, start_ms, end_ms)
        if params.apply_funding:
            funding_df = get_funding_rates(client, args.symbol, start_ms, end_ms)

    result = run_backtest(signal_df, trend_df, settings, params, funding_df)

    out = Path("data")
    out.mkdir(exist_ok=True)
    pd.DataFrame([t.__dict__ for t in result.trade_log]).to_csv(
        out / f"{args.symbol}_trades.csv", index=False
    )
    (out / f"{args.symbol}_summary.json").write_text(
        json.dumps(result.summary(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(result.summary(), indent=2, ensure_ascii=False))

    if args.walk_forward >= 2:
        print("\n=== Walk-forward ===")
        for w in walk_forward(signal_df, trend_df, settings, params, funding_df,
                              n_windows=args.walk_forward):
            r = w.result
            print(f"{w.label}: {w.start[:10]} — {w.end[:10]} | "
                  f"доходность {r.return_pct:+.2f}% | сделок {r.trades} | "
                  f"win rate {r.win_rate:.1f}% | просадка {r.max_drawdown_pct:.2f}%")


if __name__ == "__main__":
    main()
