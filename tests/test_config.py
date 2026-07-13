import dataclasses

import pytest

from src.config import Settings


def test_defaults_are_valid():
    Settings().validate()


def test_live_orders_hard_blocked():
    settings = dataclasses.replace(Settings(), enable_live_orders=True)
    with pytest.raises(ValueError, match="заблокированы"):
        settings.validate()


def test_live_trading_mode_rejected():
    settings = dataclasses.replace(Settings(), trading_mode="live")
    with pytest.raises(ValueError):
        settings.validate()


@pytest.mark.parametrize("field,value", [
    ("leverage", 4),
    ("leverage", 0),
    ("risk_per_trade", 0.05),
    ("risk_per_trade", 0.0),
    ("daily_loss_limit", 0.10),
    ("max_open_positions", 2),
    ("reward_risk", -1.0),
    ("max_trades_per_day", 0),
])
def test_invalid_values_rejected(field, value):
    settings = dataclasses.replace(Settings(), **{field: value})
    with pytest.raises(ValueError):
        settings.validate()


def test_ema_ordering_required():
    settings = dataclasses.replace(Settings(), ema_fast=50, ema_slow=20)
    with pytest.raises(ValueError, match="EMA"):
        settings.validate()
