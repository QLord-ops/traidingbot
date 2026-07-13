import pandas as pd
import pytest

from src.backtest import BacktestParams, _FundingBook, run_backtest, walk_forward
from src.config import Settings
from tests.conftest import add_crash, add_volume_spike, make_df

NO_COSTS = BacktestParams(taker_fee_rate=0.0, maker_fee_rate=0.0,
                          slippage_rate=0.0, apply_funding=False)


def make_scenario(n=300, spikes=(220,), crashes=(), gap=False):
    df = make_df(n)
    for idx in spikes:
        add_volume_spike(df, idx)
    for idx in crashes:
        add_crash(df, idx, gap=gap)
    trend = make_df(max(n // 4, 60), freq="1h")
    return df, trend


def test_backtest_runs_on_flat_data():
    settings = Settings()
    result = run_backtest(make_df(500), make_df(150, freq="1h"), settings, NO_COSTS)
    assert result.initial_balance == 1000
    assert result.final_balance > 0


def test_long_signal_opens_trade_and_conservative_stop_wins():
    """Свеча задевает и SL и TP → консервативно засчитывается SL."""
    settings = Settings()
    df, trend = make_scenario(spikes=(220,), crashes=(222,))
    result = run_backtest(df, trend, settings, NO_COSTS)
    assert result.trades == 1
    trade = result.trade_log[0]
    assert trade.side == "LONG"
    assert trade.reason == "STOP"
    assert trade.exit == pytest.approx(trade.stop)
    # без издержек убыток равен запланированному риску (0.25% от 1000)
    assert trade.pnl_usdt == pytest.approx(-2.5, rel=0.01)


def test_gap_through_stop_fills_at_open():
    """Гэп сквозь стоп исполняется по open свечи — хуже стопа."""
    settings = Settings()
    df, trend = make_scenario(spikes=(220,), crashes=(222,), gap=True)
    gap_open = float(df.loc[222, "open"])
    result = run_backtest(df, trend, settings, NO_COSTS)
    assert result.trades == 1
    trade = result.trade_log[0]
    assert trade.reason == "STOP_GAP"
    assert trade.exit == pytest.approx(gap_open)
    assert trade.pnl_usdt < -2.5  # убыток больше запланированного риска


def test_entry_on_next_candle_open():
    """Вход строго по open следующей свечи после сигнальной (нет look-ahead)."""
    settings = Settings()
    df, trend = make_scenario(spikes=(220,), crashes=(230,))
    result = run_backtest(df, trend, settings, NO_COSTS)
    assert result.trades == 1
    assert result.trade_log[0].entry == pytest.approx(float(df.loc[221, "open"]))
    assert result.trade_log[0].entry_time == str(df.loc[221, "open_time"])


def test_no_lookahead_future_data_does_not_change_past_trades():
    """Обрезка будущих данных не меняет уже закрытые сделки."""
    settings = Settings()
    df, trend = make_scenario(n=400, spikes=(220,), crashes=(222,))
    full = run_backtest(df, trend, settings, NO_COSTS)
    truncated = run_backtest(df.iloc[:260].copy(), trend, settings, NO_COSTS)
    assert [t.__dict__ for t in truncated.trade_log] == \
        [t.__dict__ for t in full.trade_log if t.exit_time <= str(df.loc[259, "close_time"])]


def test_max_trades_per_day_enforced():
    settings = Settings()
    # 4 сигнала в одном дне (96 свечей по 15m), после каждого входа — стоп
    df, trend = make_scenario(
        n=320, spikes=(220, 228, 236, 244), crashes=(222, 230, 238, 246)
    )
    result = run_backtest(df, trend, settings, NO_COSTS)
    days = {t.entry_time[:10] for t in result.trade_log}
    assert len(days) == 1
    assert result.trades == settings.max_trades_per_day == 3
    assert result.skipped_by_trade_limit >= 1


def test_daily_loss_limit_locks_entries():
    import dataclasses
    settings = dataclasses.replace(Settings(), risk_per_trade=0.01,
                                   daily_loss_limit=0.019, max_trades_per_day=10)
    settings.validate()
    df, trend = make_scenario(
        n=320, spikes=(220, 228, 236), crashes=(222, 230, 238)
    )
    result = run_backtest(df, trend, settings, NO_COSTS)
    # после двух убытков по 1% дневной лимит 1.9% блокирует третий вход
    assert result.trades == 2
    assert result.skipped_by_daily_loss >= 1


def test_funding_book_costs():
    funding = pd.DataFrame({
        "funding_time": pd.to_datetime(["2026-01-03 08:00"], utc=True),
        "funding_rate": [0.0001],
    })
    book = _FundingBook(funding)
    t0 = pd.Timestamp("2026-01-03 07:00", tz="UTC")
    t1 = pd.Timestamp("2026-01-03 09:00", tz="UTC")
    # LONG платит положительный funding
    assert book.cost(t0, t1, qty=10, price=100, side="LONG") == pytest.approx(0.1)
    # SHORT при положительной ставке получает
    assert book.cost(t0, t1, qty=10, price=100, side="SHORT") == pytest.approx(-0.1)
    # вне окна — ноль
    t2 = pd.Timestamp("2026-01-03 10:00", tz="UTC")
    assert book.cost(t1, t2, qty=10, price=100, side="LONG") == 0.0


def test_funding_applied_to_open_position():
    settings = Settings()
    df, trend = make_scenario(n=400, spikes=(220,), crashes=(300,))
    entry_time = df.loc[221, "open_time"]
    funding = pd.DataFrame({
        "funding_time": [entry_time + pd.Timedelta(hours=2)],
        "funding_rate": [0.01],  # нарочно крупная ставка
    })
    params = BacktestParams(taker_fee_rate=0.0, maker_fee_rate=0.0,
                            slippage_rate=0.0, apply_funding=True)
    result = run_backtest(df, trend, settings, params, funding_df=funding)
    assert result.trades == 1
    assert result.total_funding > 0
    assert result.trade_log[0].funding_usdt == pytest.approx(result.total_funding)


def test_metrics_and_curves_present():
    settings = Settings()
    df, trend = make_scenario(n=400, spikes=(220, 260), crashes=(222, 262))
    result = run_backtest(df, trend, settings, NO_COSTS)
    assert result.trades == 2
    assert result.max_consecutive_losses == 2
    assert result.expectancy_usdt < 0
    assert result.avg_r == pytest.approx(-1.0, rel=0.05)
    assert len(result.equity_curve) > 0
    assert len(result.drawdown_curve) == len(result.equity_curve)
    assert result.max_drawdown_pct > 0
    assert result.monthly_returns  # хотя бы один месяц
    summary = result.summary()
    assert "trade_log" not in summary and "equity_curve" not in summary


def test_walk_forward_windows():
    settings = Settings()
    df, trend = make_scenario(n=900, spikes=(300, 500, 700),
                              crashes=(302, 502, 702))
    windows = walk_forward(df, trend, settings, NO_COSTS, n_windows=3)
    assert len(windows) == 3
    total = sum(w.result.trades for w in windows)
    assert total >= 1
    for w in windows:
        assert w.start < w.end
