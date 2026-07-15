from src.journal import Journal
from src.testnet_engine import TestnetEngine
from tests.conftest import add_volume_spike, make_df
from tests.test_engine import FakeClient, make_testnet_settings


class TwoSymbolClient(FakeClient):
    """BTC с сильным трендом, ETH со слабым; сигнал donchian у обоих."""

    def __init__(self):
        super().__init__()
        strong = make_df(300, slope=0.5)   # цена далеко над EMA200
        weak = make_df(300, slope=0.05)
        for df in (strong, weak):
            df.loc[298, "close"] = df.loc[298, "open"] + 5.0  # пробой канала
            df.loc[298, "high"] = df.loc[298, "close"] + 0.5
            add_volume_spike(df, 298)
        self.by_symbol = {
            "BTCUSDT": (strong, make_df(100, freq="1h", slope=0.5)),
            "ETHUSDT": (weak, make_df(100, freq="1h", slope=0.05)),
        }

    def klines(self, symbol, interval, limit=500, **kw):
        signal_df, trend_df = self.by_symbol[symbol]
        return (trend_df if interval == "1h" else signal_df).copy()

    def exchange_info(self):
        info = super().exchange_info()
        info["symbols"].append({**info["symbols"][0], "symbol": "ETHUSDT"})
        return info


def test_engine_enters_strongest_signal_first(tmp_path):
    settings = make_testnet_settings(
        strategy_mode="donchian", symbols=("ETHUSDT", "BTCUSDT"),
        macro_filter=False,
    )
    client = TwoSymbolClient()
    engine = TestnetEngine(settings, client, Journal(str(tmp_path / "t.db")))
    engine.prepare()
    engine.run_cycle()

    entries = [o for o in client.market_orders if not o["reduce_only"]]
    assert len(entries) == 1  # одна позиция на весь счёт
    assert entries[0]["symbol"] == "BTCUSDT"  # выбран сильнейший тренд
    assert engine.status.candidates
    assert engine.status.candidates[0]["symbol"] == "BTCUSDT"
    assert engine.status.candidates[0]["strength"] >= \
        engine.status.candidates[-1]["strength"]
