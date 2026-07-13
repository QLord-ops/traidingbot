from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

import pandas as pd
import requests


class BinanceAPIError(RuntimeError):
    pass


class BinanceFuturesClient:
    """Минимальный REST-клиент Binance USDⓈ-M Futures."""

    LIVE_BASE_URL = "https://fapi.binance.com"
    TESTNET_BASE_URL = "https://demo-fapi.binance.com"

    def __init__(self, api_key: str = "", api_secret: str = "", testnet: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.base_url = self.TESTNET_BASE_URL if testnet else self.LIVE_BASE_URL
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"X-MBX-APIKEY": api_key})

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False) -> dict | list:
        params = dict(params or {})
        if signed:
            if not self.api_key or not self.api_secret:
                raise BinanceAPIError("Для signed-запроса нужны API key и secret")
            params["timestamp"] = int(time.time() * 1000)
            params.setdefault("recvWindow", 5000)
            query = urlencode(params, doseq=True)
            params["signature"] = hmac.new(
                self.api_secret, query.encode(), hashlib.sha256
            ).hexdigest()

        try:
            response = self.session.request(
                method, f"{self.base_url}{path}", params=params, timeout=15
            )
        except requests.RequestException as exc:
            raise BinanceAPIError(f"Ошибка сети: {exc}") from exc

        if not response.ok:
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
            raise BinanceAPIError(
                f"Binance HTTP {response.status_code}: {payload}"
            )
        return response.json()

    def ping(self) -> bool:
        self._request("GET", "/fapi/v1/ping")
        return True

    def server_time(self) -> int:
        payload = self._request("GET", "/fapi/v1/time")
        return int(payload["serverTime"])

    def klines(self, symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
        raw = self._request(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        columns = [
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ]
        df = pd.DataFrame(raw, columns=columns)
        numeric = ["open", "high", "low", "close", "volume", "quote_volume"]
        df[numeric] = df[numeric].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        return df

    def account(self) -> dict:
        return self._request("GET", "/fapi/v3/account", signed=True)

    def balance_usdt(self) -> float:
        account = self.account()
        for asset in account.get("assets", []):
            if asset.get("asset") == "USDT":
                return float(asset.get("availableBalance", 0))
        return 0.0

    def exchange_info(self) -> dict:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def commission_rate(self, symbol: str) -> dict:
        return self._request(
            "GET", "/fapi/v1/commissionRate", {"symbol": symbol}, signed=True
        )

    def change_leverage(self, symbol: str, leverage: int) -> dict:
        return self._request(
            "POST", "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": leverage}, signed=True,
        )

    def change_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        return self._request(
            "POST", "/fapi/v1/marginType",
            {"symbol": symbol, "marginType": margin_type}, signed=True,
        )

    def position_risk(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        payload = self._request("GET", "/fapi/v3/positionRisk", params, signed=True)
        return payload if isinstance(payload, list) else [payload]

    def new_market_order(self, symbol: str, side: str, quantity: float,
                         reduce_only: bool = False) -> dict:
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def new_algo_order(self, symbol: str, side: str, order_type: str,
                       trigger_price: float, close_position: bool = True,
                       working_type: str = "MARK_PRICE") -> dict:
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "triggerPrice": trigger_price,
            "workingType": working_type,
            "priceProtect": "TRUE",
        }
        if close_position:
            params["closePosition"] = "true"
        return self._request("POST", "/fapi/v1/algoOrder", params, signed=True)
