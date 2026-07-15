import pytest

from src.journal import Journal
from src.testnet_engine import TestnetEngine, _utc_day
from tests.test_engine import FakeClient, make_testnet_settings


def make_engine(tmp_path):
    journal = Journal(str(tmp_path / "t.db"))
    engine = TestnetEngine(make_testnet_settings(), FakeClient(), journal)
    return engine, journal


def add_closed_trades(journal, n, pnl, r_denominator=5.0):
    """n закрытых сделок с заданным PnL (риск = qty·|entry−stop| = 5)."""
    day = _utc_day()
    for k in range(n):
        cid = f"tb-conf-{pnl}-{k}"
        journal.record_trade_open(day, "BTCUSDT", "LONG", cid, 1.0, 100.0, 95.0, None)
        journal.close_trade(cid, "CLOSED", realized_pnl=pnl)


def test_conformity_waits_for_min_trades(tmp_path):
    engine, journal = make_engine(tmp_path)
    add_closed_trades(journal, 5, pnl=-5.0)
    engine.check_conformity()
    assert engine.risk_scale == 1.0
    assert "наблюдение" in engine.status.conformity


def test_conformity_halves_risk_on_underperformance(tmp_path):
    engine, journal = make_engine(tmp_path)
    # 25 сделок по −1R: avg R = −1.0, граница ≈ 0.233 − 1.645·1.73/√25 ≈ −0.34
    add_closed_trades(journal, 25, pnl=-5.0)
    engine.check_conformity()
    assert engine.risk_scale == 0.5
    events = journal.recent_events(5)
    assert any("снижен вдвое" in e["message"] for e in events)


def test_conformity_restores_risk_on_recovery(tmp_path):
    engine, journal = make_engine(tmp_path)
    add_closed_trades(journal, 25, pnl=-5.0)
    engine.check_conformity()
    assert engine.risk_scale == 0.5
    # серия прибыльных возвращает средний R в интервал
    add_closed_trades(journal, 40, pnl=10.0)
    engine.check_conformity()
    assert engine.risk_scale == 1.0


def test_conformity_ok_within_band(tmp_path):
    engine, journal = make_engine(tmp_path)
    add_closed_trades(journal, 15, pnl=5.0)   # +1R
    add_closed_trades(journal, 10, pnl=-5.0)  # −1R → avg +0.2R, в интервале
    engine.check_conformity()
    assert engine.risk_scale == 1.0


def test_execution_recording(tmp_path):
    journal = Journal(str(tmp_path / "t.db"))
    journal.record_execution("BTCUSDT", "tb-1", "LONG",
                             signal_price=100.0, fill_price=100.05)
    journal.record_execution("BTCUSDT", "tb-2", "SHORT",
                             signal_price=100.0, fill_price=99.9)
    stats = journal.execution_stats()
    assert stats["count"] == 2
    # LONG: купили на 5 б.п. дороже сигнала (хуже) = +5;
    # SHORT: продали на 10 б.п. ниже сигнала (тоже хуже) = +10
    assert stats["avg_bps"] == pytest.approx(7.5)
    assert stats["worst_bps"] == pytest.approx(10.0)


def test_engine_records_fill(tmp_path):
    engine, journal = make_engine(tmp_path)
    engine.prepare()
    client = engine.client
    client.market_orders.clear()

    original = client.new_market_order

    def with_avg_price(*args, **kwargs):
        result = original(*args, **kwargs)
        result["avgPrice"] = "115.5"
        return result

    client.new_market_order = with_avg_price
    assert engine.process_symbol("BTCUSDT") == "OPENED"
    stats = journal.execution_stats()
    assert stats["count"] == 1
