from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .binance_client import BinanceFuturesClient, BinanceAPIError
from .config import Settings, MAX_LEVERAGE
from .journal import Journal
from .market_rules import SymbolRules, parse_symbol_rules, round_to_tick
from .risk import calculate_position
from .strategy import Side, evaluate

log = logging.getLogger(__name__)


class EngineError(RuntimeError):
    pass


class LiveTradingBlocked(EngineError):
    """Любая попытка направить engine на боевой API."""


@dataclass
class EngineStatus:
    running: bool = False
    mode: str = "testnet"
    started_at: str | None = None
    last_cycle_at: str | None = None
    last_error: str | None = None
    day_locked: bool = False
    trades_today: int = 0
    realized_pnl_today: float = 0.0
    balance_usdt: float = 0.0
    positions: list[dict] = field(default_factory=list)


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class TestnetEngine:
    """Исполнение сигналов на Binance Futures Testnet (Demo Trading).

    Гарантии:
    - работает ТОЛЬКО против demo-fapi (боевой base_url вызывает исключение);
    - идемпотентные clientOrderId: рестарт не создаёт дублирующую позицию;
    - вход без подтверждённого SL немедленно закрывается (emergency close);
    - дневной лимит убытка и лимит числа сделок блокируют новые входы;
    - reconciliation при старте: чужая/оставшаяся позиция без SL закрывается.
    """

    __test__ = False  # не собирать классом pytest (имя начинается с Test)

    def __init__(self, settings: Settings, client: BinanceFuturesClient,
                 journal: Journal):
        settings.validate()
        if settings.trading_mode != "testnet":
            raise EngineError("Engine запускается только в TRADING_MODE=testnet")
        if client.base_url != BinanceFuturesClient.TESTNET_BASE_URL:
            raise LiveTradingBlocked(
                "Engine отказывается работать с боевым API. Live-режим заблокирован."
            )
        if settings.leverage > MAX_LEVERAGE:
            raise EngineError(f"Плечо выше {MAX_LEVERAGE}x запрещено")
        self.settings = settings
        self.client = client
        self.journal = journal
        self.rules: dict[str, SymbolRules] = {}
        self.status = EngineStatus()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # --- подготовка -------------------------------------------------------

    def prepare(self) -> None:
        self.client.sync_time()
        if self.client.position_mode():
            raise EngineError(
                "Аккаунт в Hedge Mode. Переключите на One-way в настройках Binance."
            )
        info = self.client.exchange_info()
        for symbol in self.settings.symbols:
            self.rules[symbol] = parse_symbol_rules(info, symbol)
            self.client.change_margin_type(symbol, "ISOLATED")
            self.client.change_leverage(symbol, self.settings.leverage)
        self.journal.log_event("INFO", "Engine подготовлен: isolated, leverage="
                               f"{self.settings.leverage}, symbols={self.settings.symbols}")

    # --- reconciliation -----------------------------------------------------

    def reconcile(self) -> None:
        """Синхронизация с реальным состоянием биржи при старте/переподключении."""
        for symbol in self.settings.symbols:
            positions = [
                p for p in self.client.position_risk(symbol)
                if abs(float(p.get("positionAmt", 0) or 0)) > 0
            ]
            algo_orders = self.client.open_algo_orders(symbol)
            has_stop = any(
                o.get("type") in ("STOP_MARKET", "STOP") for o in algo_orders
            )
            if positions and not has_stop:
                self.journal.log_event(
                    "CRITICAL",
                    f"{symbol}: позиция без Stop Loss обнаружена при reconciliation — "
                    "аварийное закрытие",
                )
                self.emergency_close(symbol)
            elif not positions and algo_orders:
                # осиротевшие защитные ордера без позиции
                self.journal.log_event(
                    "WARNING", f"{symbol}: осиротевшие algo-ордера без позиции — отмена"
                )
                for order in algo_orders:
                    self.client.cancel_algo_order(symbol, algo_id=order.get("algoId"))
            elif positions:
                self.journal.log_event(
                    "INFO", f"{symbol}: найдена защищённая позиция, принимаем сопровождение"
                )
        # закрытые на бирже, но открытые в журнале сделки
        for trade in self.journal.open_trades():
            symbol = trade["symbol"]
            open_amt = sum(
                abs(float(p.get("positionAmt", 0) or 0))
                for p in self.client.position_risk(symbol)
            )
            if open_amt == 0:
                pnl = self._realized_pnl_since(symbol, trade["created_at"])
                self.journal.close_trade(trade["client_order_id"], "CLOSED_ON_EXCHANGE", pnl)
                self.journal.log_event(
                    "INFO", f"{symbol}: сделка {trade['client_order_id']} закрыта биржей, "
                    f"PnL={pnl}"
                )

    def _realized_pnl_since(self, symbol: str, created_at: str) -> float:
        try:
            start_ms = int(datetime.fromisoformat(created_at)
                           .replace(tzinfo=timezone.utc).timestamp() * 1000)
            income = self.client.income_history(
                symbol=symbol, income_type="REALIZED_PNL", start_time=start_ms
            )
            return sum(float(x.get("income", 0)) for x in income)
        except (BinanceAPIError, ValueError) as exc:
            log.warning("Не удалось получить realized PnL: %s", exc)
            return 0.0

    # --- риск-лимиты ---------------------------------------------------------

    def _daily_limits_ok(self, balance: float) -> tuple[bool, str]:
        day = _utc_day()
        trades_today = self.journal.trades_on_day(day)
        realized = self.journal.realized_pnl_on_day(day)
        self.status.trades_today = trades_today
        self.status.realized_pnl_today = realized
        day_start_balance = balance - realized
        if day_start_balance > 0 and realized <= -day_start_balance * self.settings.daily_loss_limit:
            self.status.day_locked = True
            return False, (f"дневной лимит убытка достигнут: {realized:.2f} USDT "
                           f"({realized / day_start_balance * 100:.2f}%)")
        self.status.day_locked = False
        if trades_today >= self.settings.max_trades_per_day:
            return False, f"достигнут лимит сделок за день ({trades_today})"
        return True, ""

    def _has_open_position(self) -> bool:
        for symbol in self.settings.symbols:
            for p in self.client.position_risk(symbol):
                if abs(float(p.get("positionAmt", 0) or 0)) > 0:
                    return True
        return False

    # --- исполнение -----------------------------------------------------------

    def process_symbol(self, symbol: str) -> str:
        """Один шаг: оценить сигнал и, если можно, открыть защищённую позицию."""
        signal_df = self.client.klines(symbol, self.settings.signal_interval,
                                       self.settings.kline_limit)
        trend_df = self.client.klines(symbol, self.settings.trend_interval,
                                      self.settings.kline_limit)
        signal = evaluate(symbol, signal_df, trend_df, self.settings)
        self.journal.save_signal(signal)
        if signal.side == Side.HOLD:
            return "HOLD"

        candle_ms = int(pd_ts_to_ms(signal.candle_time))
        client_order_id = f"tb-{symbol}-{candle_ms}"
        if self.journal.has_trade(client_order_id):
            return "DUPLICATE_SKIPPED"  # идемпотентность: свеча уже отработана

        balance = self.client.balance_usdt()
        self.status.balance_usdt = balance
        ok, why = self._daily_limits_ok(balance)
        if not ok:
            self.journal.log_event("INFO", f"{symbol}: вход пропущен — {why}")
            return "LIMITS"
        if self._has_open_position():
            return "POSITION_EXISTS"

        rules = self.rules[symbol]
        plan = calculate_position(
            balance, self.settings.risk_per_trade, signal.entry, signal.stop,
            self.settings.leverage, rules=rules,
        )
        stop_price = round_to_tick(signal.stop, rules.tick_size)
        tp_price = round_to_tick(signal.take_profit, rules.tick_size)
        order_side = "BUY" if signal.side == Side.LONG else "SELL"
        close_side = "SELL" if signal.side == Side.LONG else "BUY"

        # Журналируем ДО отправки: при обрыве после отправки рестарт не продублирует вход
        self.journal.record_trade_open(
            _utc_day(), symbol, signal.side.value, client_order_id,
            plan.quantity, signal.entry, stop_price, tp_price,
        )
        try:
            entry_order = self.client.new_market_order(
                symbol, order_side, plan.quantity, client_order_id=client_order_id
            )
        except BinanceAPIError as exc:
            # Неизвестно, принят ли ордер — проверяем по идемпотентному ID
            entry_order = self._order_status_or_none(symbol, client_order_id)
            if entry_order is None:
                self.journal.close_trade(client_order_id, "ENTRY_FAILED", 0.0)
                self.journal.log_event("ERROR", f"{symbol}: вход не исполнен: {exc}")
                return "ENTRY_FAILED"

        # --- защитные ордера: без подтверждённого SL позиция не живёт ---------
        try:
            self.client.new_algo_order(
                symbol, close_side, "STOP_MARKET", stop_price,
                close_position=True, client_algo_id=f"{client_order_id}-sl",
            )
        except BinanceAPIError as exc:
            self.journal.log_event(
                "CRITICAL", f"{symbol}: SL не подтверждён ({exc}) — аварийное закрытие"
            )
            self.emergency_close(symbol)
            self.journal.close_trade(client_order_id, "EMERGENCY_CLOSED", None)
            return "EMERGENCY_CLOSED"

        try:
            self.client.new_algo_order(
                symbol, close_side, "TAKE_PROFIT_MARKET", tp_price,
                close_position=True, client_algo_id=f"{client_order_id}-tp",
            )
        except BinanceAPIError as exc:
            self.journal.log_event(
                "CRITICAL", f"{symbol}: TP не подтверждён ({exc}) — аварийное закрытие"
            )
            self.emergency_close(symbol)
            self.journal.close_trade(client_order_id, "EMERGENCY_CLOSED", None)
            return "EMERGENCY_CLOSED"

        self.journal.log_event(
            "INFO",
            f"{symbol}: открыта позиция {signal.side.value} qty={plan.quantity} "
            f"SL={stop_price} TP={tp_price} (id={client_order_id})",
        )
        return "OPENED"

    def _order_status_or_none(self, symbol: str, client_order_id: str) -> dict | None:
        try:
            order = self.client.get_order(symbol, client_order_id)
            if order.get("status") in ("FILLED", "PARTIALLY_FILLED", "NEW"):
                return order
        except BinanceAPIError:
            pass
        return None

    def emergency_close(self, symbol: str) -> None:
        """Отменяет все ордера символа и закрывает позицию reduce-only market."""
        try:
            for order in self.client.open_algo_orders(symbol):
                try:
                    self.client.cancel_algo_order(symbol, algo_id=order.get("algoId"))
                except BinanceAPIError as exc:
                    log.warning("Отмена algo-ордера %s: %s", order.get("algoId"), exc)
            self.client.cancel_all_orders(symbol)
        except BinanceAPIError as exc:
            log.warning("Отмена ордеров %s: %s", symbol, exc)
        for p in self.client.position_risk(symbol):
            amt = float(p.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            side = "SELL" if amt > 0 else "BUY"
            self.client.new_market_order(
                symbol, side, abs(amt), reduce_only=True,
                client_order_id=f"tb-emergency-{symbol}-{int(time.time() * 1000)}",
            )
        self.journal.log_event("WARNING", f"{symbol}: emergency close выполнен")

    def emergency_close_all(self) -> None:
        for symbol in self.settings.symbols:
            self.emergency_close(symbol)

    # --- жизненный цикл ---------------------------------------------------------

    def run_cycle(self) -> None:
        self.reconcile()
        for symbol in self.settings.symbols:
            result = self.process_symbol(symbol)
            log.info("%s: %s", symbol, result)
        self.status.positions = [
            {
                "symbol": p.get("symbol"),
                "amt": p.get("positionAmt"),
                "entry": p.get("entryPrice"),
                "unrealized": p.get("unRealizedProfit"),
            }
            for s in self.settings.symbols
            for p in self.client.position_risk(s)
            if abs(float(p.get("positionAmt", 0) or 0)) > 0
        ]
        self.status.last_cycle_at = datetime.now(timezone.utc).isoformat()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.prepare()
            self.reconcile()
            self._stop.clear()
            self.status.running = True
            self.status.started_at = datetime.now(timezone.utc).isoformat()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            self.journal.log_event("INFO", "Engine запущен")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_cycle()
                self.status.last_error = None
            except LiveTradingBlocked:
                raise
            except Exception as exc:  # цикл не должен умирать от единичной ошибки
                self.status.last_error = str(exc)
                self.journal.log_event("ERROR", f"Цикл engine: {exc}")
                log.exception("Ошибка цикла engine")
            self._stop.wait(self.settings.poll_seconds)
        self.status.running = False

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
        self.status.running = False
        self.journal.log_event("INFO", "Engine остановлен")


def pd_ts_to_ms(ts: str) -> int:
    import pandas as pd
    return int(pd.Timestamp(ts).timestamp() * 1000)
