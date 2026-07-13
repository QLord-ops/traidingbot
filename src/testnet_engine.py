from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .binance_client import BinanceFuturesClient, BinanceAPIError
from .config import Settings, MAX_LEVERAGE
from .indicators import add_indicators
from .journal import Journal
from .market_rules import SymbolRules, parse_symbol_rules, round_to_tick
from .risk import calculate_position
from .strategy import Side, chandelier_stop, evaluate

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
                 journal: Journal, notifier=None):
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
        self.notifier = notifier  # опциональный TelegramNotifier (duck typing: .send)
        self.rules: dict[str, SymbolRules] = {}
        self.status = EngineStatus()
        self._seen_candles: set[str] = set()
        self._adopted: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _event(self, level: str, message: str, notify: bool = True) -> None:
        """Журналирует событие и (для значимых) шлёт уведомление владельцу."""
        self.journal.log_event(level, message)
        if notify and self.notifier is not None:
            self.notifier.send(f"[{level}] {message}")

    def status_text(self) -> str:
        st = self.status
        positions = ", ".join(
            f"{p['symbol']} {p['amt']} (uPnL {p['unrealized']})" for p in st.positions
        ) or "нет"
        return (
            f"Engine: {'работает' if st.running else 'остановлен'}\n"
            f"Баланс: {st.balance_usdt:.2f} USDT\n"
            f"Сделок сегодня: {st.trades_today}, PnL сегодня: {st.realized_pnl_today:+.2f}\n"
            f"Дневная блокировка: {'ДА' if st.day_locked else 'нет'}\n"
            f"Позиции: {positions}\n"
            f"Последний цикл: {st.last_cycle_at or '—'}"
            + (f"\nОшибка: {st.last_error}" if st.last_error else "")
        )

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
        self._event("INFO", "Engine подготовлен: isolated, leverage="
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
                self._event(
                    "CRITICAL",
                    f"{symbol}: позиция без Stop Loss обнаружена при reconciliation — "
                    "аварийное закрытие",
                )
                self.emergency_close(symbol)
                self._adopted.discard(symbol)
            elif not positions and algo_orders:
                # осиротевшие защитные ордера без позиции
                self._event(
                    "WARNING", f"{symbol}: осиротевшие algo-ордера без позиции — отмена"
                )
                for order in algo_orders:
                    self.client.cancel_algo_order(symbol, algo_id=order.get("algoId"))
                self._adopted.discard(symbol)
            elif positions:
                # уведомляем один раз, а не каждый цикл reconcile
                if symbol not in self._adopted:
                    self._adopted.add(symbol)
                    self._event(
                        "INFO", f"{symbol}: найдена защищённая позиция, принимаем сопровождение"
                    )
            else:
                self._adopted.discard(symbol)
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
                self._event(
                    "INFO", f"{symbol}: сделка {trade['client_order_id']} закрыта биржей, "
                    f"PnL={pnl:+.2f} USDT"
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
        if client_order_id in self._seen_candles or self.journal.has_trade(client_order_id):
            return "DUPLICATE_SKIPPED"  # идемпотентность: свеча уже отработана
        # Каждая сигнальная свеча обрабатывается один раз, каким бы ни был исход:
        # без этого заблокированный лимитами сигнал спамил бы журнал каждый цикл.
        self._seen_candles.add(client_order_id)
        if len(self._seen_candles) > 2000:
            self._seen_candles.clear()

        balance = self.client.balance_usdt()
        self.status.balance_usdt = balance
        ok, why = self._daily_limits_ok(balance)
        if not ok:
            self._event("INFO", f"{symbol}: вход пропущен — {why}")
            return "LIMITS"
        if self._has_open_position():
            return "POSITION_EXISTS"

        rules = self.rules[symbol]
        plan = calculate_position(
            balance, self.settings.risk_per_trade, signal.entry, signal.stop,
            self.settings.leverage, rules=rules,
        )
        stop_price = round_to_tick(signal.stop, rules.tick_size)
        tp_price = (round_to_tick(signal.take_profit, rules.tick_size)
                    if signal.take_profit is not None else None)
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
                self._event("ERROR", f"{symbol}: вход не исполнен: {exc}")
                return "ENTRY_FAILED"

        # --- защитные ордера: без подтверждённого SL позиция не живёт ---------
        try:
            self.client.new_algo_order(
                symbol, close_side, "STOP_MARKET", stop_price,
                close_position=True, client_algo_id=f"{client_order_id}-sl",
            )
        except BinanceAPIError as exc:
            self._event(
                "CRITICAL", f"{symbol}: SL не подтверждён ({exc}) — аварийное закрытие"
            )
            self.emergency_close(symbol)
            self.journal.close_trade(client_order_id, "EMERGENCY_CLOSED", None)
            return "EMERGENCY_CLOSED"

        # TP только для скоринговой стратегии; donchian выходит по трейлинг-стопу
        if tp_price is not None:
            try:
                self.client.new_algo_order(
                    symbol, close_side, "TAKE_PROFIT_MARKET", tp_price,
                    close_position=True, client_algo_id=f"{client_order_id}-tp",
                )
            except BinanceAPIError as exc:
                self._event(
                    "CRITICAL", f"{symbol}: TP не подтверждён ({exc}) — аварийное закрытие"
                )
                self.emergency_close(symbol)
                self.journal.close_trade(client_order_id, "EMERGENCY_CLOSED", None)
                return "EMERGENCY_CLOSED"

        self._event(
            "INFO",
            f"{symbol}: открыта позиция {signal.side.value} qty={plan.quantity} "
            f"SL={stop_price} TP={tp_price if tp_price is not None else 'трейлинг'} "
            f"(id={client_order_id})",
        )
        return "OPENED"

    # --- трейлинг-стоп (donchian) ------------------------------------------

    def manage_trailing_stops(self) -> None:
        """Подтягивает защитный STOP_MARKET за ценой (Chandelier) для donchian.

        Modify условных ордеров Binance не поддерживает, поэтому стоп двигается
        заменой: сначала ставится новый (более тесный) стоп, затем отменяется
        старый — позиция ни на миг не остаётся без защиты. Стоп только тесним,
        никогда не ослабляем.
        """
        if self.settings.strategy_mode != "donchian":
            return
        for symbol in self.settings.symbols:
            try:
                self._trail_symbol(symbol)
            except BinanceAPIError as exc:
                self._event("ERROR", f"{symbol}: трейлинг-стоп не обновлён: {exc}",
                            notify=False)

    def _trail_symbol(self, symbol: str) -> None:
        positions = [
            p for p in self.client.position_risk(symbol)
            if abs(float(p.get("positionAmt", 0) or 0)) > 0
        ]
        if not positions:
            return
        amt = float(positions[0].get("positionAmt", 0))
        side = Side.LONG if amt > 0 else Side.SHORT
        close_side = "SELL" if side == Side.LONG else "BUY"

        stops = [o for o in self.client.open_algo_orders(symbol)
                 if o.get("type") == "STOP_MARKET"]
        if not stops:
            return  # без защиты — этим занимается reconcile, не трейлинг
        current = stops[0]
        current_trigger = float(current.get("triggerPrice", 0) or 0)

        df = self.client.klines(symbol, self.settings.signal_interval,
                                self.settings.atr_period + 5)
        s = add_indicators(df, self.settings.ema_fast, self.settings.ema_slow,
                           self.settings.ema_trend, self.settings.atr_period,
                           self.settings.volume_period)
        cur = s.iloc[-2]  # последняя закрытая свеча
        atr = float(cur["atr"])
        ref = float(cur["close"])
        rules = self.rules[symbol]
        new_stop = round_to_tick(
            chandelier_stop(side, ref, atr, self.settings.trail_atr_mult),
            rules.tick_size,
        )
        # тесним только в благоприятную сторону
        improves = new_stop > current_trigger if side == Side.LONG else new_stop < current_trigger
        if not improves:
            return

        new_id = f"tb-{symbol}-trail-{int(time.time() * 1000)}"
        try:
            self.client.new_algo_order(
                symbol, close_side, "STOP_MARKET", new_stop,
                close_position=True, client_algo_id=new_id,
            )
        except BinanceAPIError as exc:
            # -2021: цена уже дошла до нового стопа — закрываем позицию немедленно
            if getattr(exc, "code", None) == -2021:
                self._event("WARNING",
                            f"{symbol}: цена достигла трейлинг-уровня — закрытие")
                self.emergency_close(symbol)
            else:
                self._event("ERROR",
                            f"{symbol}: не удалось поставить новый стоп ({exc}) — "
                            "старый стоп сохранён", notify=False)
            return
        # новый стоп принят — теперь безопасно снять старый
        try:
            self.client.cancel_algo_order(symbol, algo_id=current.get("algoId"))
        except BinanceAPIError as exc:
            log.warning("Не удалось снять старый стоп %s: %s",
                        current.get("algoId"), exc)
        self._event("INFO",
                    f"{symbol}: трейлинг-стоп подтянут {current_trigger} → {new_stop}",
                    notify=False)

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
        self._event("WARNING", f"{symbol}: emergency close выполнен")

    def emergency_close_all(self) -> None:
        for symbol in self.settings.symbols:
            self.emergency_close(symbol)

    # --- жизненный цикл ---------------------------------------------------------

    def run_cycle(self) -> None:
        self.reconcile()
        self.manage_trailing_stops()
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
            self._event("INFO", "Engine запущен")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_cycle()
                self.status.last_error = None
            except LiveTradingBlocked:
                raise
            except Exception as exc:  # цикл не должен умирать от единичной ошибки
                message = str(exc)
                # одинаковая повторяющаяся ошибка не спамит журнал и Telegram
                if message != self.status.last_error:
                    self._event("ERROR", f"Цикл engine: {message}")
                self.status.last_error = message
                log.exception("Ошибка цикла engine")
            self._stop.wait(self.settings.poll_seconds)
        self.status.running = False

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
        self.status.running = False
        self._event("INFO", "Engine остановлен")


def pd_ts_to_ms(ts: str) -> int:
    import pandas as pd
    return int(pd.Timestamp(ts).timestamp() * 1000)
