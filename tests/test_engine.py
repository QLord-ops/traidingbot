import dataclasses

import pytest

from src.binance_client import BinanceAPIError, BinanceFuturesClient
from src.config import Settings
from src.journal import Journal
from src.testnet_engine import EngineError, LiveTradingBlocked, TestnetEngine, _utc_day
from tests.conftest import add_volume_spike, make_df


def make_testnet_settings(**overrides) -> Settings:
    base = Settings()
    fields = {
        "trading_mode": "testnet", "api_key": "key", "api_secret": "secret",
        "symbols": ("BTCUSDT",),
    }
    fields.update(overrides)
    return dataclasses.replace(base, **fields)


class FakeClient:
    base_url = BinanceFuturesClient.TESTNET_BASE_URL

    def __init__(self, fail_sl=False, fail_tp=False):
        self.fail_sl = fail_sl
        self.fail_tp = fail_tp
        self.market_orders: list[dict] = []
        self.algo_orders_placed: list[dict] = []
        self.cancelled_algo: list = []
        self.cancelled_all: list = []
        self.positions: list[dict] = []
        self.open_algo: list[dict] = []
        # сигнальные данные: LONG на последней закрытой свече (iloc[-2])
        self.signal_df = make_df(300)
        add_volume_spike(self.signal_df, 298)
        self.trend_df = make_df(100, freq="1h")

    # --- публичные ---
    def klines(self, symbol, interval, limit=500, **kw):
        return (self.trend_df if interval == "1h" else self.signal_df).copy()

    def exchange_info(self):
        return {"symbols": [{"symbol": "BTCUSDT", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001",
             "minQty": "0.001", "maxQty": "1000"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ]}]}

    def sync_time(self):
        return 0

    # --- аккаунт ---
    def balance_usdt(self):
        return 1000.0

    def position_mode(self):
        return False  # one-way

    def change_margin_type(self, symbol, margin_type="ISOLATED"):
        return {}

    def change_leverage(self, symbol, leverage):
        return {}

    def position_risk(self, symbol=None):
        return self.positions

    def income_history(self, **kw):
        return []

    # --- ордера ---
    def new_market_order(self, symbol, side, quantity, reduce_only=False,
                         client_order_id=None):
        order = {"symbol": symbol, "side": side, "qty": quantity,
                 "reduce_only": reduce_only, "client_order_id": client_order_id}
        self.market_orders.append(order)
        if not reduce_only:
            amt = quantity if side == "BUY" else -quantity
            self.positions = [{"symbol": symbol, "positionAmt": str(amt),
                               "entryPrice": "100", "unRealizedProfit": "0"}]
        else:
            self.positions = []
        return {"status": "FILLED", **order}

    def get_order(self, symbol, client_order_id):
        raise BinanceAPIError("Order does not exist", code=-2013)

    def new_algo_order(self, symbol, side, order_type, trigger_price,
                       close_position=True, working_type="MARK_PRICE",
                       client_algo_id=None):
        if order_type == "STOP_MARKET" and self.fail_sl:
            raise BinanceAPIError("rejected", code=-2021)
        if order_type == "TAKE_PROFIT_MARKET" and self.fail_tp:
            raise BinanceAPIError("rejected", code=-2021)
        order = {"symbol": symbol, "side": side, "type": order_type,
                 "triggerPrice": trigger_price, "algoId": len(self.algo_orders_placed) + 1,
                 "clientAlgoId": client_algo_id}
        self.algo_orders_placed.append(order)
        self.open_algo.append(order)
        return order

    def open_algo_orders(self, symbol=None):
        return self.open_algo

    def cancel_algo_order(self, symbol, algo_id=None, client_algo_id=None):
        self.cancelled_algo.append(algo_id or client_algo_id)
        self.open_algo = [o for o in self.open_algo if o.get("algoId") != algo_id]
        return {}

    def cancel_all_orders(self, symbol):
        self.cancelled_all.append(symbol)
        return {}


def make_engine(tmp_path, client=None, **settings_overrides):
    client = client or FakeClient()
    journal = Journal(str(tmp_path / "test.db"))
    engine = TestnetEngine(make_testnet_settings(**settings_overrides), client, journal)
    engine.prepare()
    return engine, client, journal


def test_engine_refuses_live_client(tmp_path):
    client = FakeClient()
    client.base_url = BinanceFuturesClient.LIVE_BASE_URL
    with pytest.raises(LiveTradingBlocked):
        TestnetEngine(make_testnet_settings(), client, Journal(str(tmp_path / "t.db")))


def test_engine_refuses_dry_run_mode(tmp_path):
    with pytest.raises(EngineError):
        TestnetEngine(make_testnet_settings(trading_mode="dry_run"), FakeClient(),
                      Journal(str(tmp_path / "t.db")))


def test_entry_places_protective_orders(tmp_path):
    engine, client, journal = make_engine(tmp_path)
    result = engine.process_symbol("BTCUSDT")
    assert result == "OPENED"
    assert len(client.market_orders) == 1
    assert client.market_orders[0]["client_order_id"].startswith("tb-BTCUSDT-")
    types = {o["type"] for o in client.algo_orders_placed}
    assert types == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    assert len(journal.open_trades()) == 1


def test_restart_does_not_duplicate_position(tmp_path):
    engine, client, journal = make_engine(tmp_path)
    assert engine.process_symbol("BTCUSDT") == "OPENED"
    client.positions = []  # даже если позиции нет — свеча уже отработана
    engine2 = TestnetEngine(make_testnet_settings(), client, journal)
    engine2.rules = engine.rules
    assert engine2.process_symbol("BTCUSDT") == "DUPLICATE_SKIPPED"
    assert len(client.market_orders) == 1


def test_sl_failure_triggers_emergency_close(tmp_path):
    engine, client, journal = make_engine(tmp_path, client=FakeClient(fail_sl=True))
    result = engine.process_symbol("BTCUSDT")
    assert result == "EMERGENCY_CLOSED"
    assert client.positions == []  # позиция закрыта
    reduce_orders = [o for o in client.market_orders if o["reduce_only"]]
    assert len(reduce_orders) == 1
    assert journal.open_trades() == []


def test_tp_failure_also_closes_position(tmp_path):
    engine, client, journal = make_engine(tmp_path, client=FakeClient(fail_tp=True))
    result = engine.process_symbol("BTCUSDT")
    assert result == "EMERGENCY_CLOSED"
    assert client.positions == []


def test_reconcile_closes_unprotected_position(tmp_path):
    client = FakeClient()
    client.positions = [{"symbol": "BTCUSDT", "positionAmt": "0.5",
                         "entryPrice": "100", "unRealizedProfit": "0"}]
    engine, client, journal = make_engine(tmp_path, client=client)
    engine.reconcile()
    assert client.positions == []
    assert any(o["reduce_only"] for o in client.market_orders)


def test_reconcile_cancels_orphan_algo_orders(tmp_path):
    client = FakeClient()
    client.open_algo = [{"symbol": "BTCUSDT", "type": "STOP_MARKET", "algoId": 7}]
    engine, client, journal = make_engine(tmp_path, client=client)
    engine.reconcile()
    assert 7 in client.cancelled_algo


def test_daily_loss_limit_blocks_entries(tmp_path):
    engine, client, journal = make_engine(tmp_path)
    day = _utc_day()
    journal.record_trade_open(day, "BTCUSDT", "LONG", "tb-x-1", 1, 100, 99, 102)
    journal.close_trade("tb-x-1", "CLOSED", realized_pnl=-100.0)  # −10% за день
    ok, why = engine._daily_limits_ok(balance=900.0)
    assert not ok
    assert "дневной лимит" in why
    assert engine.process_symbol("BTCUSDT") == "LIMITS"


def test_max_trades_per_day_blocks_entries(tmp_path):
    engine, client, journal = make_engine(tmp_path)
    day = _utc_day()
    for k in range(3):
        journal.record_trade_open(day, "BTCUSDT", "LONG", f"tb-x-{k}", 1, 100, 99, 102)
        journal.close_trade(f"tb-x-{k}", "CLOSED", realized_pnl=0.5)
    ok, why = engine._daily_limits_ok(balance=1000.0)
    assert not ok
    assert "лимит сделок" in why
