from __future__ import annotations

import logging
import time

from .binance_client import BinanceFuturesClient, BinanceAPIError
from .config import Settings
from .journal import Journal
from .strategy import evaluate, Side


def run_once(settings: Settings, client: BinanceFuturesClient,
             journal: Journal) -> None:
    for symbol in settings.symbols:
        signal_df = client.klines(symbol, settings.signal_interval, settings.kline_limit)
        trend_df = client.klines(symbol, settings.trend_interval, settings.kline_limit)
        signal = evaluate(symbol, signal_df, trend_df, settings)
        is_new = journal.save_signal(signal)

        if is_new:
            logging.info(
                "%s | %s | score=%s | entry=%s | SL=%s | TP=%s | %s",
                signal.symbol, signal.side.value, signal.score,
                signal.entry, signal.stop, signal.take_profit, signal.reason
            )
            if signal.side != Side.HOLD:
                logging.warning(
                    "НАЙДЕН СИГНАЛ. Версия 0.1 НЕ отправляет реальный ордер."
                )


def main() -> None:
    settings = Settings()
    settings.validate()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    client = BinanceFuturesClient(
        api_key=settings.api_key,
        api_secret=settings.api_secret,
        testnet=settings.trading_mode == "testnet",
    )
    journal = Journal()

    logging.info(
        "Запуск: mode=%s symbols=%s",
        settings.trading_mode, ",".join(settings.symbols)
    )
    client.ping()
    if settings.api_key and settings.api_secret:
        client.sync_time()

    while True:
        try:
            run_once(settings, client, journal)
        except BinanceAPIError as exc:
            logging.error("%s", exc)
        except Exception:
            logging.exception("Необработанная ошибка")
        time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    main()
