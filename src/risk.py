from __future__ import annotations
from dataclasses import dataclass
import math

from .market_rules import SymbolRules


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
    rules: SymbolRules | None = None,
) -> PositionPlan:
    if balance_usdt <= 0:
        raise ValueError("Баланс должен быть положительным")
    distance = abs(entry - stop)
    if distance <= 0:
        raise ValueError("Стоп должен отличаться от цены входа")

    risk_usdt = balance_usdt * risk_fraction
    raw_quantity = risk_usdt / distance
    step = rules.step_size if rules and rules.step_size > 0 else quantity_step
    quantity = math.floor(raw_quantity / step) * step
    if quantity <= 0:
        raise ValueError("Размер позиции меньше минимального шага")

    notional = quantity * entry
    margin = notional / leverage
    if margin > balance_usdt:
        raise ValueError("Недостаточно баланса для маржи позиции")

    if rules:
        if rules.min_qty and quantity < rules.min_qty:
            raise ValueError(
                f"Количество {quantity} меньше minQty={rules.min_qty} для {rules.symbol}"
            )
        if rules.max_qty and quantity > rules.max_qty:
            raise ValueError(
                f"Количество {quantity} больше maxQty={rules.max_qty} для {rules.symbol}"
            )
        if rules.min_notional and notional < rules.min_notional:
            raise ValueError(
                f"Номинал {notional:.2f} меньше minNotional={rules.min_notional} "
                f"для {rules.symbol}"
            )
    return PositionPlan(quantity, risk_usdt, notional, margin)
