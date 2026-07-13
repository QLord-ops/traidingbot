from src.market_rules import parse_symbol_rules, floor_to_step, round_to_tick


def test_rounding():
    assert floor_to_step(1.239, 0.01) == 1.23
    assert round_to_tick(100.126, 0.1) == 100.1


def test_parse_rules():
    info = {"symbols": [{"symbol": "BTCUSDT", "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
    ]}]}
    rules = parse_symbol_rules(info, "BTCUSDT")
    assert rules.tick_size == 0.1
    assert rules.step_size == 0.001
    assert rules.min_notional == 5
