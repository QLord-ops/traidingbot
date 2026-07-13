from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    tick_size: float
    step_size: float
    min_qty: float
    max_qty: float
    min_notional: float


def _quantize(value: float, step: float, rounding) -> float:
    if step <= 0:
        return value
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    units = (d_value / d_step).to_integral_value(rounding=rounding)
    return float(units * d_step)


def floor_to_step(value: float, step: float) -> float:
    return _quantize(value, step, ROUND_DOWN)


def round_to_tick(value: float, tick: float) -> float:
    return _quantize(value, tick, ROUND_HALF_UP)


def parse_symbol_rules(exchange_info: dict, symbol: str) -> SymbolRules:
    item = next((x for x in exchange_info.get("symbols", []) if x.get("symbol") == symbol), None)
    if not item:
        raise ValueError(f"Символ {symbol} не найден в exchangeInfo")
    filters = {f["filterType"]: f for f in item.get("filters", [])}
    price = filters.get("PRICE_FILTER", {})
    lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE", {})
    notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL", {})
    min_notional = notional.get("notional", notional.get("minNotional", 0))
    return SymbolRules(
        symbol=symbol,
        tick_size=float(price.get("tickSize", 0)),
        step_size=float(lot.get("stepSize", 0)),
        min_qty=float(lot.get("minQty", 0)),
        max_qty=float(lot.get("maxQty", 0)),
        min_notional=float(min_notional or 0),
    )
