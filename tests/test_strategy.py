import pytest

from src.config import Settings
from src.indicators import add_indicators
from src.strategy import Side, evaluate, protective_levels, score_candle
from tests.conftest import add_volume_spike, make_df


def prepared(df, settings):
    return add_indicators(df, settings.ema_fast, settings.ema_slow,
                          settings.ema_trend, settings.atr_period,
                          settings.volume_period)


def test_nan_indicators_give_zero_score():
    settings = Settings()
    s = prepared(make_df(30), settings)  # мало данных → NaN в volume_avg
    trend = {"close": 100, "ema_fast": 99, "ema_slow": 98, "ema_trend": 97}
    score = score_candle(s.iloc[5], s.iloc[4], trend, settings)
    assert score.long_score == 0 and score.short_score == 0
    assert score.side() == Side.HOLD


def test_uptrend_with_volume_gives_long():
    settings = Settings()
    df = make_df(300)
    add_volume_spike(df, 298)
    s = prepared(df, settings)
    cur, prev = s.iloc[298], s.iloc[297]
    trend_df = prepared(make_df(100, freq="1h"), settings)
    t = trend_df.iloc[-2]
    trend = {"close": t["close"], "ema_fast": t["ema_fast"],
             "ema_slow": t["ema_slow"], "ema_trend": t["ema_trend"]}
    score = score_candle(cur, prev, trend, settings)
    assert score.side() == Side.LONG
    assert score.long_score >= 75


def test_evaluate_uses_last_closed_candle():
    settings = Settings()
    df = make_df(300)
    add_volume_spike(df, 298)  # предпоследняя строка = последняя закрытая
    signal = evaluate("BTCUSDT", df, make_df(100, freq="1h"), settings)
    assert signal.side == Side.LONG
    assert signal.entry == pytest.approx(float(df.loc[298, "close"]))
    assert signal.stop < signal.entry < signal.take_profit


def test_evaluate_last_closed_flag():
    settings = Settings()
    df = make_df(300)
    add_volume_spike(df, 299)  # последняя строка закрыта (кэш/CSV)
    signal = evaluate("BTCUSDT", df, make_df(100, freq="1h"), settings,
                      last_closed=True)
    assert signal.side == Side.LONG


def test_protective_levels_long_short():
    stop, tp = protective_levels(Side.LONG, entry=100.0, prev_low=99.5,
                                 prev_high=100.5, atr=1.0, reward_risk=2.0)
    assert stop == pytest.approx(98.7)  # entry - 1.3*atr < prev_low
    assert tp == pytest.approx(100 + (100 - 98.7) * 2)

    stop_s, tp_s = protective_levels(Side.SHORT, entry=100.0, prev_low=99.5,
                                     prev_high=100.5, atr=1.0, reward_risk=2.0)
    assert stop_s == pytest.approx(101.3)
    assert tp_s == pytest.approx(100 - 1.3 * 2)
