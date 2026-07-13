from __future__ import annotations

from dataclasses import dataclass, field
import os
from dotenv import load_dotenv

load_dotenv()

MAX_LEVERAGE = 3  # согласовано в HANDOFF_RU.md: начальное плечо не выше 3x


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _str(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    # default_factory: env читается при создании экземпляра, а не при импорте модуля
    api_key: str = field(default_factory=lambda: _str("BINANCE_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: _str("BINANCE_API_SECRET", ""))
    trading_mode: str = field(default_factory=lambda: _str("TRADING_MODE", "dry_run").lower())

    symbols: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip().upper()
        for s in _str("SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
        if s.strip()
    ))
    signal_interval: str = field(default_factory=lambda: _str("SIGNAL_INTERVAL", "15m"))
    trend_interval: str = field(default_factory=lambda: _str("TREND_INTERVAL", "1h"))
    kline_limit: int = field(default_factory=lambda: _int("KLINE_LIMIT", 500))

    risk_per_trade: float = field(default_factory=lambda: _float("RISK_PER_TRADE", 0.0025))
    daily_loss_limit: float = field(default_factory=lambda: _float("DAILY_LOSS_LIMIT", 0.015))
    max_trades_per_day: int = field(default_factory=lambda: _int("MAX_TRADES_PER_DAY", 3))
    max_open_positions: int = field(default_factory=lambda: _int("MAX_OPEN_POSITIONS", 1))
    leverage: int = field(default_factory=lambda: _int("LEVERAGE", 3))
    reward_risk: float = field(default_factory=lambda: _float("REWARD_RISK", 1.8))

    # score — интрадей-скоринг (исходная гипотеза); donchian — трендовый
    # breakout на старшем ТФ с трейлинг-стопом (пока только backtest)
    strategy_mode: str = field(default_factory=lambda: _str("STRATEGY_MODE", "score").lower())
    donchian_period: int = field(default_factory=lambda: _int("DONCHIAN_PERIOD", 48))
    trail_atr_mult: float = field(default_factory=lambda: _float("TRAIL_ATR_MULT", 3.0))

    ema_fast: int = field(default_factory=lambda: _int("EMA_FAST", 20))
    ema_slow: int = field(default_factory=lambda: _int("EMA_SLOW", 50))
    ema_trend: int = field(default_factory=lambda: _int("EMA_TREND", 200))
    atr_period: int = field(default_factory=lambda: _int("ATR_PERIOD", 14))
    atr_min_pct: float = field(default_factory=lambda: _float("ATR_MIN_PCT", 0.002))
    volume_period: int = field(default_factory=lambda: _int("VOLUME_PERIOD", 20))
    volume_multiplier: float = field(default_factory=lambda: _float("VOLUME_MULTIPLIER", 1.15))

    poll_seconds: int = field(default_factory=lambda: _int("POLL_SECONDS", 30))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO"))

    telegram_bot_token: str = field(default_factory=lambda: _str("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _str("TELEGRAM_CHAT_ID", ""))

    enable_live_orders: bool = field(
        default_factory=lambda: _str("ENABLE_LIVE_ORDERS", "false").lower() == "true"
    )
    live_confirmation: str = field(default_factory=lambda: _str("LIVE_CONFIRMATION", ""))

    def validate(self) -> None:
        if self.enable_live_orders:
            raise ValueError(
                "Реальные ордера жёстко заблокированы: допускается только dry_run и testnet"
            )
        if self.trading_mode not in {"dry_run", "testnet"}:
            raise ValueError("Допускается только TRADING_MODE=dry_run или testnet")
        if not 0 < self.risk_per_trade <= 0.01:
            raise ValueError("RISK_PER_TRADE должен быть > 0 и <= 1%")
        if not 0 < self.daily_loss_limit <= 0.05:
            raise ValueError("DAILY_LOSS_LIMIT должен быть > 0 и <= 5%")
        if not 1 <= self.leverage <= MAX_LEVERAGE:
            raise ValueError(f"LEVERAGE должен быть от 1 до {MAX_LEVERAGE}")
        if not 1 <= self.max_trades_per_day <= 10:
            raise ValueError("MAX_TRADES_PER_DAY должен быть от 1 до 10")
        if self.max_open_positions != 1:
            raise ValueError("Пока допускается только одна открытая позиция")
        if self.reward_risk <= 0:
            raise ValueError("REWARD_RISK должен быть положительным")
        if not 0 < self.ema_fast < self.ema_slow < self.ema_trend:
            raise ValueError("Требуется 0 < EMA_FAST < EMA_SLOW < EMA_TREND")
        if self.atr_period < 2 or self.volume_period < 2:
            raise ValueError("ATR_PERIOD и VOLUME_PERIOD должны быть >= 2")
        if self.atr_min_pct < 0:
            raise ValueError("ATR_MIN_PCT не может быть отрицательным")
        if self.volume_multiplier <= 0:
            raise ValueError("VOLUME_MULTIPLIER должен быть положительным")
        if self.strategy_mode not in {"score", "donchian"}:
            raise ValueError("STRATEGY_MODE должен быть score или donchian")
        if self.donchian_period < 10:
            raise ValueError("DONCHIAN_PERIOD должен быть >= 10")
        if self.trail_atr_mult <= 0:
            raise ValueError("TRAIL_ATR_MULT должен быть положительным")
