import dataclasses

from src.binance_client import BinanceAPIError, BinanceFuturesClient
from src.journal import Journal
from src.strategy import Side, evaluate
from src.testnet_engine import TestnetEngine
from tests.conftest import make_df
from tests.test_engine import make_testnet_settings


def donchian_testnet_settings(**overrides):
    fields = {"strategy_mode": "donchian", "donchian_period": 20,
              "trail_atr_mult": 3.0, "signal_interval": "4h",
              "trend_interval": "1d"}
    fields.update(overrides)
    return dataclasses.replace(make_testnet_settings(), **fields)


class TrailingFakeClient:
    base_url = BinanceFuturesClient.TESTNET_BASE_URL

    def __init__(self, position_amt=0.5, current_stop_trigger=1.0,
                 fail_new_2021=False):
        self.position_amt = position_amt
        self.stops = [{"type": "STOP_MARKET", "algoId": 1,
                       "triggerPrice": current_stop_trigger}]
        self.new_orders: list[dict] = []
        self.cancelled: list = []
        self.market_orders: list[dict] = []
        self.kdf = make_df(60, slope=0.5)  # растущий тренд → ATR определён

    def klines(self, symbol, interval, limit=500, **kw):
        return self.kdf.copy()

    def exchange_info(self):
        return {"symbols": [{"symbol": "BTCUSDT", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001",
             "minQty": "0.001", "maxQty": "1000"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ]}]}

    def sync_time(self):
        return 0

    def position_mode(self):
        return False

    def change_margin_type(self, symbol, margin_type="ISOLATED"):
        return {}

    def change_leverage(self, symbol, leverage):
        return {}

    def balance_usdt(self):
        return 1000.0

    def income_history(self, **kw):
        return []

    def position_risk(self, symbol=None):
        if self.position_amt == 0:
            return []
        return [{"symbol": symbol, "positionAmt": str(self.position_amt),
                 "entryPrice": "100", "unRealizedProfit": "0"}]

    def open_algo_orders(self, symbol=None):
        return list(self.stops)

    def new_algo_order(self, symbol, side, order_type, trigger_price,
                       close_position=True, working_type="MARK_PRICE",
                       client_algo_id=None):
        if self._fail_2021:
            raise BinanceAPIError("would immediately trigger", code=-2021)
        order = {"type": order_type, "algoId": len(self.new_orders) + 10,
                 "triggerPrice": trigger_price, "clientAlgoId": client_algo_id}
        self.new_orders.append(order)
        self.stops.append(order)
        return order

    _fail_2021 = False

    def cancel_algo_order(self, symbol, algo_id=None, client_algo_id=None):
        self.cancelled.append(algo_id)
        self.stops = [s for s in self.stops if s.get("algoId") != algo_id]
        return {}

    def cancel_all_orders(self, symbol):
        return {}

    def new_market_order(self, symbol, side, quantity, reduce_only=False,
                         client_order_id=None):
        self.market_orders.append({"side": side, "reduce_only": reduce_only})
        if reduce_only:
            self.position_amt = 0
        return {"status": "FILLED"}


def build_engine(client, tmp_path, **overrides):
    engine = TestnetEngine(donchian_testnet_settings(**overrides), client,
                           Journal(str(tmp_path / "t.db")))
    engine.prepare()
    return engine


def test_trailing_tightens_stop(tmp_path):
    client = TrailingFakeClient(current_stop_trigger=1.0)
    engine = build_engine(client, tmp_path)
    engine.manage_trailing_stops()
    assert len(client.new_orders) == 1
    assert client.new_orders[0]["type"] == "STOP_MARKET"
    new_trigger = client.new_orders[0]["triggerPrice"]
    assert new_trigger > 1.0            # стоп подтянут вверх
    assert 1 in client.cancelled        # старый стоп снят ПОСЛЕ постановки нового


def test_trailing_never_loosens(tmp_path):
    # текущий стоп уже выше нового Chandelier → ничего не меняем
    client = TrailingFakeClient(current_stop_trigger=1000.0)
    engine = build_engine(client, tmp_path)
    engine.manage_trailing_stops()
    assert client.new_orders == []
    assert client.cancelled == []


def test_trailing_places_new_before_cancelling_old(tmp_path):
    """Инвариант: новый стоп ставится раньше отмены старого (нет окна без защиты)."""
    client = TrailingFakeClient(current_stop_trigger=1.0)
    order_log = []
    orig_new = client.new_algo_order
    orig_cancel = client.cancel_algo_order

    def traced_new(*a, **k):
        order_log.append("place")
        return orig_new(*a, **k)

    def traced_cancel(*a, **k):
        order_log.append("cancel")
        return orig_cancel(*a, **k)

    client.new_algo_order = traced_new
    client.cancel_algo_order = traced_cancel
    engine = build_engine(client, tmp_path)
    engine.manage_trailing_stops()
    assert order_log == ["place", "cancel"]


def test_trailing_2021_closes_position(tmp_path):
    client = TrailingFakeClient(current_stop_trigger=1.0)
    client._fail_2021 = True
    engine = build_engine(client, tmp_path)
    engine.manage_trailing_stops()
    # цена уже дошла до трейлинг-уровня → позиция закрыта reduce-only
    assert client.position_amt == 0
    assert any(o["reduce_only"] for o in client.market_orders)


def test_trailing_noop_without_position(tmp_path):
    client = TrailingFakeClient(position_amt=0)
    engine = build_engine(client, tmp_path)
    engine.manage_trailing_stops()
    assert client.new_orders == []


def test_trailing_noop_in_score_mode(tmp_path):
    client = TrailingFakeClient(current_stop_trigger=1.0)
    engine = TestnetEngine(
        dataclasses.replace(donchian_testnet_settings(), strategy_mode="score"),
        client, Journal(str(tmp_path / "t.db")),
    )
    engine.rules = {"BTCUSDT": engine.rules.get("BTCUSDT")}  # без prepare
    engine.manage_trailing_stops()
    assert client.new_orders == []


def test_live_donchian_signal_long():
    """evaluate() в donchian-режиме отдаёт LONG на пробое канала без TP."""
    settings = donchian_testnet_settings()
    df = make_df(60, slope=0.0)          # боковик → узкий канал
    for i in (58, 59):                    # пробой только на последней закрытой свече
        for col in ("open", "high", "low", "close"):
            df.loc[i, col] += 5.0
    trend = make_df(40, freq="1d", slope=0.5)
    signal = evaluate("BTCUSDT", df, trend, settings)
    assert signal.side == Side.LONG
    assert signal.take_profit is None
    assert signal.stop < signal.entry
