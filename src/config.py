from __future__ import annotations

from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv("BINANCE_API_KEY", "")
    api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    trading_mode: str = os.getenv("TRADING_MODE", "dry_run").lower()

    symbols: tuple[str, ...] = tuple(
        s.strip().upper()
        for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
        if s.strip()
    )
    signal_interval: str = os.getenv("SIGNAL_INTERVAL", "15m")
    trend_interval: str = os.getenv("TREND_INTERVAL", "1h")
    kline_limit: int = _int("KLINE_LIMIT", 500)

    risk_per_trade: float = _float("RISK_PER_TRADE", 0.0025)
    daily_loss_limit: float = _float("DAILY_LOSS_LIMIT", 0.015)
    max_trades_per_day: int = _int("MAX_TRADES_PER_DAY", 3)
    max_open_positions: int = _int("MAX_OPEN_POSITIONS", 1)
    leverage: int = _int("LEVERAGE", 3)
    reward_risk: float = _float("REWARD_RISK", 1.8)

    ema_fast: int = _int("EMA_FAST", 20)
    ema_slow: int = _int("EMA_SLOW", 50)
    ema_trend: int = _int("EMA_TREND", 200)
    atr_period: int = _int("ATR_PERIOD", 14)
    atr_min_pct: float = _float("ATR_MIN_PCT", 0.002)
    volume_period: int = _int("VOLUME_PERIOD", 20)
    volume_multiplier: float = _float("VOLUME_MULTIPLIER", 1.15)

    poll_seconds: int = _int("POLL_SECONDS", 30)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    enable_live_orders: bool = os.getenv("ENABLE_LIVE_ORDERS", "false").lower() == "true"
    live_confirmation: str = os.getenv("LIVE_CONFIRMATION", "")

    def validate(self) -> None:
        if self.trading_mode not in {"dry_run", "testnet"}:
            raise ValueError("v0.1 допускает только TRADING_MODE=dry_run или testnet")
        if not 0 < self.risk_per_trade <= 0.01:
            raise ValueError("RISK_PER_TRADE должен быть > 0 и <= 1%")
        if not 0 < self.daily_loss_limit <= 0.05:
            raise ValueError("DAILY_LOSS_LIMIT должен быть > 0 и <= 5%")
        if not 1 <= self.leverage <= 5:
            raise ValueError("В первой версии LEVERAGE должен быть от 1 до 5")
        if self.enable_live_orders:
            raise ValueError("Реальные ордера заблокированы в версии 0.1")
