from __future__ import annotations

import argparse
import json
from pathlib import Path
import pandas as pd

from .backtest import run_backtest
from .binance_client import BinanceFuturesClient
from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--limit", type=int, default=1500)
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--fee", type=float, default=0.0005)
    parser.add_argument("--slippage", type=float, default=0.0002)
    parser.add_argument("--signal-csv")
    parser.add_argument("--trend-csv")
    args = parser.parse_args()

    settings = Settings()
    settings.validate()
    if args.signal_csv and args.trend_csv:
        signal_df = pd.read_csv(args.signal_csv, parse_dates=["open_time", "close_time"])
        trend_df = pd.read_csv(args.trend_csv, parse_dates=["open_time", "close_time"])
    else:
        client = BinanceFuturesClient(testnet=False)
        signal_df = client.klines(args.symbol, settings.signal_interval, min(args.limit, 1500))
        trend_df = client.klines(args.symbol, settings.trend_interval, min(args.limit, 1500))
    result = run_backtest(signal_df, trend_df, settings, args.balance, args.fee, args.slippage)

    out = Path("data")
    out.mkdir(exist_ok=True)
    pd.DataFrame([t.__dict__ for t in result.trade_log]).to_csv(
        out / f"{args.symbol}_trades.csv", index=False
    )
    (out / f"{args.symbol}_summary.json").write_text(
        json.dumps(result.summary(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(result.summary(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
