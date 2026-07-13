import pytest
from src.risk import calculate_position


def test_position_risk():
    plan = calculate_position(
        balance_usdt=1000,
        risk_fraction=0.0025,
        entry=100,
        stop=99,
        leverage=3,
        quantity_step=0.001,
    )
    assert plan.quantity == 2.5
    assert plan.risk_usdt == 2.5
    assert plan.notional_usdt == 250
    assert plan.margin_estimate_usdt == pytest.approx(83.3333333)
