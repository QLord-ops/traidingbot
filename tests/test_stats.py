import pytest

from src.journal import Journal
from src.stats import compute_trade_stats


def make_trade(symbol="BTCUSDT", pnl=10.0, qty=1.0, entry=100.0, stop=95.0,
               status="CLOSED_ON_EXCHANGE"):
    return {"symbol": symbol, "side": "LONG", "qty": qty, "entry": entry,
            "stop": stop, "realized_pnl": pnl, "status": status,
            "created_at": "2026-07-14 10:00:00"}


def test_empty_stats():
    stats = compute_trade_stats([])
    assert stats.trades == 0
    assert stats.win_rate == 0.0
    assert stats.profit_factor == 0.0


def test_basic_aggregates():
    trades = [
        make_trade(pnl=10.0),               # +2R (риск 5)
        make_trade(pnl=-5.0),               # −1R
        make_trade(pnl=-5.0, symbol="ETHUSDT"),
        make_trade(pnl=15.0, symbol="ETHUSDT"),  # +3R
    ]
    stats = compute_trade_stats(trades)
    assert stats.trades == 4
    assert stats.wins == 2 and stats.losses == 2
    assert stats.win_rate == 50.0
    assert stats.total_pnl == pytest.approx(15.0)
    assert stats.expectancy == pytest.approx(3.75)
    assert stats.avg_r == pytest.approx((2 - 1 - 1 + 3) / 4)
    assert stats.profit_factor == pytest.approx(25.0 / 10.0)
    assert stats.max_consecutive_losses == 2
    assert stats.by_symbol["BTCUSDT"]["trades"] == 2
    assert stats.by_symbol["ETHUSDT"]["pnl"] == pytest.approx(10.0)


def test_open_trades_excluded():
    trades = [make_trade(pnl=None), make_trade(pnl=5.0)]
    stats = compute_trade_stats(trades)
    assert stats.trades == 1


def test_zero_risk_r_skipped():
    # entry == stop: R не считается, но PnL учитывается
    stats = compute_trade_stats([make_trade(pnl=5.0, entry=100.0, stop=100.0)])
    assert stats.trades == 1
    assert stats.avg_r == 0.0
    assert stats.total_pnl == pytest.approx(5.0)


def test_journal_closed_trades(tmp_path):
    journal = Journal(str(tmp_path / "t.db"))
    journal.record_trade_open("2026-07-14", "BTCUSDT", "LONG", "tb-1",
                              1.0, 100.0, 95.0, None)
    journal.record_trade_open("2026-07-14", "BTCUSDT", "LONG", "tb-2",
                              1.0, 100.0, 95.0, None)
    journal.close_trade("tb-1", "CLOSED_ON_EXCHANGE", realized_pnl=7.5)

    closed = journal.closed_trades()
    assert len(closed) == 1  # открытая tb-2 не попадает
    assert closed[0]["client_order_id"] == "tb-1"
    assert closed[0]["realized_pnl"] == pytest.approx(7.5)

    stats = compute_trade_stats(closed)
    assert stats.trades == 1
    assert stats.avg_r == pytest.approx(1.5)
