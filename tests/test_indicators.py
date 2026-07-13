import pandas as pd
from src.indicators import add_indicators


def test_indicators_are_added():
    df = pd.DataFrame({
        "high": [11, 12, 13, 14, 15],
        "low": [9, 10, 11, 12, 13],
        "close": [10, 11, 12, 13, 14],
        "volume": [100, 110, 120, 130, 140],
    })
    out = add_indicators(df, 2, 3, 4, 3, 3)
    for col in ["ema_fast", "ema_slow", "ema_trend", "atr", "atr_pct", "volume_avg"]:
        assert col in out.columns
