from __future__ import annotations
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class PositionPlan:
    quantity: float
    risk_usdt: float
    notional_usdt: float
    margin_estimate_usdt: float


def calculate_position(
    balance_usdt: float,
    risk_fraction: float,
    entry: float,
    stop: float,
    leverage: int,
    quantity_step: float = 0.001,
) -> PositionPlan:
    if balance_usdt <= 0:
        raise ValueError("Баланс должен быть положительным")
    distance = abs(entry - stop)
    if distance <= 0:
        raise ValueError("Стоп должен отличаться от цены входа")

    risk_usdt = balance_usdt * risk_fraction
    raw_quantity = risk_usdt / distance
    quantity = math.floor(raw_quantity / quantity_step) * quantity_step
    if quantity <= 0:
        raise ValueError("Размер позиции меньше минимального шага")

    notional = quantity * entry
    margin = notional / leverage
    return PositionPlan(quantity, risk_usdt, notional, margin)
