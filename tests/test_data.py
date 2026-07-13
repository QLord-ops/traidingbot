import pandas as pd

from src import data as data_mod
from src.data import fetch_klines_range, get_klines
from tests.conftest import make_df


class FakePublicClient:
    """Отдаёт срезы заранее сгенерированного набора свечей, как Binance API."""

    def __init__(self, df: pd.DataFrame, page_limit: int = 1500):
        self.df = df
        self.page_limit = page_limit
        self.calls = 0

    def klines(self, symbol, interval, limit=500, start_time=None, end_time=None):
        self.calls += 1
        df = self.df
        if start_time is not None:
            df = df[df["open_time"] >= pd.Timestamp(start_time, unit="ms", tz="UTC")]
        if end_time is not None:
            df = df[df["open_time"] <= pd.Timestamp(end_time, unit="ms", tz="UTC")]
        return df.head(min(limit, self.page_limit)).reset_index(drop=True)


def ms(ts) -> int:
    return int(pd.Timestamp(ts).timestamp() * 1000)


def test_pagination_stitches_pages():
    df = make_df(4000, freq="15min")
    client = FakePublicClient(df)
    out = fetch_klines_range(client, "BTCUSDT", "15m",
                             ms(df["open_time"].iloc[0]),
                             ms(df["open_time"].iloc[-1]) + 1)
    assert len(out) == 4000
    assert client.calls >= 3  # 4000 / 1500 → минимум 3 страницы
    assert out["open_time"].is_monotonic_increasing
    assert not out["open_time"].duplicated().any()


def test_cache_avoids_refetch(tmp_path, monkeypatch):
    monkeypatch.setattr(data_mod, "CACHE_DIR", tmp_path)
    df = make_df(2000, freq="15min")
    start, end = ms(df["open_time"].iloc[0]), ms(df["open_time"].iloc[-1]) + 1

    client1 = FakePublicClient(df)
    out1 = get_klines(client1, "BTCUSDT", "15m", start, end)
    assert len(out1) == 2000
    assert (tmp_path / "BTCUSDT_15m.csv").exists()

    client2 = FakePublicClient(df)
    out2 = get_klines(client2, "BTCUSDT", "15m", start, end)
    assert client2.calls == 0  # всё из кэша
    assert len(out2) == 2000
    pd.testing.assert_frame_equal(
        out1.reset_index(drop=True), out2.reset_index(drop=True), check_dtype=False
    )


def test_cache_extends_forward(tmp_path, monkeypatch):
    monkeypatch.setattr(data_mod, "CACHE_DIR", tmp_path)
    df = make_df(2000, freq="15min")
    start = ms(df["open_time"].iloc[0])
    mid = ms(df["open_time"].iloc[999]) + 1
    end = ms(df["open_time"].iloc[-1]) + 1

    client = FakePublicClient(df)
    first = get_klines(client, "BTCUSDT", "15m", start, mid)
    assert len(first) == 1000

    calls_before = client.calls
    full = get_klines(client, "BTCUSDT", "15m", start, end)
    assert len(full) == 2000
    assert client.calls > calls_before  # докачаны только новые свечи


def test_only_closed_candles_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(data_mod, "CACHE_DIR", tmp_path)
    # свечи "сейчас": последняя ещё не закрыта и должна быть отброшена
    now = pd.Timestamp.now(tz="UTC").floor("15min")
    df = make_df(50, freq="15min", start=str(now - pd.Timedelta(minutes=15 * 49)))
    client = FakePublicClient(df)
    out = get_klines(client, "BTCUSDT", "15m",
                     ms(df["open_time"].iloc[0]),
                     ms(df["open_time"].iloc[-1]) + 1, use_cache=False)
    assert len(out) < 50
    assert (out["close_time"] < pd.Timestamp.now(tz="UTC")).all()
