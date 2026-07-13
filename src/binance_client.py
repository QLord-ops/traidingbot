from __future__ import annotations

import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import pandas as pd
import requests

log = logging.getLogger(__name__)

# Максимум свечей за один запрос /fapi/v1/klines (официальный лимит Binance)
KLINES_MAX_LIMIT = 1500

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


class BinanceAPIError(RuntimeError):
    """Ошибка Binance API с кодом (если Binance его вернул)."""

    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class BinanceFuturesClient:
    """Минимальный REST-клиент Binance USDⓈ-M Futures.

    Testnet = официальный Demo Trading (demo-fapi.binance.com), который заменил
    testnet.binancefuture.com. Условные ордера (SL/TP) с 2025-12-09 размещаются
    только через /fapi/v1/algoOrder (см. changelog developers.binance.com).
    """

    LIVE_BASE_URL = "https://fapi.binance.com"
    TESTNET_BASE_URL = "https://demo-fapi.binance.com"

    def __init__(self, api_key: str = "", api_secret: str = "", testnet: bool = False,
                 max_retries: int = 3):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.base_url = self.TESTNET_BASE_URL if testnet else self.LIVE_BASE_URL
        self.max_retries = max_retries
        self._time_offset_ms = 0
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"X-MBX-APIKEY": api_key})

    # --- инфраструктура -------------------------------------------------

    def sync_time(self) -> int:
        """Синхронизирует локальное время с сервером (защита от ошибки -1021)."""
        server = self.server_time()
        self._time_offset_ms = server - int(time.time() * 1000)
        log.info("Синхронизация времени: offset=%d мс", self._time_offset_ms)
        return self._time_offset_ms

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False) -> dict | list:
        params = {k: v for k, v in dict(params or {}).items() if v is not None}
        if signed:
            if not self.api_key or not self.api_secret:
                raise BinanceAPIError("Для signed-запроса нужны API key и secret")
            params["timestamp"] = self._timestamp()
            params.setdefault("recvWindow", 5000)
            query = urlencode(params, doseq=True)
            params["signature"] = hmac.new(
                self.api_secret, query.encode(), hashlib.sha256
            ).hexdigest()

        last_error: BinanceAPIError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(
                    method, f"{self.base_url}{path}", params=params, timeout=15
                )
            except requests.RequestException as exc:
                last_error = BinanceAPIError(f"Ошибка сети: {exc}")
                # POST-ордера НЕ повторяем вслепую: неизвестно, принят ли ордер.
                # Идемпотентность обеспечивает newClientOrderId/clientAlgoId на уровне engine.
                if method != "GET":
                    raise last_error from exc
                time.sleep(min(2 ** attempt, 8))
                continue

            if response.ok:
                return response.json()

            try:
                payload = response.json()
                code = payload.get("code") if isinstance(payload, dict) else None
                msg = payload.get("msg", str(payload)) if isinstance(payload, dict) else str(payload)
            except ValueError:
                code, msg = None, response.text

            last_error = BinanceAPIError(
                f"Binance HTTP {response.status_code} (code={code}): {msg}",
                status=response.status_code, code=code,
            )
            # 429/418 — rate limit / ban: обязательный backoff по документации
            if response.status_code in (429, 418) and attempt < self.max_retries:
                retry_after = int(response.headers.get("Retry-After", 0) or 0)
                time.sleep(max(retry_after, 2 ** attempt))
                continue
            # -1021: рассинхронизация времени — синхронизируемся и повторяем signed GET
            if code == -1021 and signed and method == "GET" and attempt < self.max_retries:
                self.sync_time()
                params.pop("signature", None)
                params["timestamp"] = self._timestamp()
                query = urlencode({k: v for k, v in params.items() if k != "signature"}, doseq=True)
                params["signature"] = hmac.new(
                    self.api_secret, query.encode(), hashlib.sha256
                ).hexdigest()
                continue
            raise last_error
        raise last_error  # исчерпаны попытки

    # --- публичные данные ------------------------------------------------

    def ping(self) -> bool:
        self._request("GET", "/fapi/v1/ping")
        return True

    def server_time(self) -> int:
        payload = self._request("GET", "/fapi/v1/time")
        return int(payload["serverTime"])

    def klines(self, symbol: str, interval: str, limit: int = 500,
               start_time: int | None = None, end_time: int | None = None) -> pd.DataFrame:
        raw = self._request(
            "GET",
            "/fapi/v1/klines",
            {
                "symbol": symbol, "interval": interval,
                "limit": min(limit, KLINES_MAX_LIMIT),
                "startTime": start_time, "endTime": end_time,
            },
        )
        return klines_to_dataframe(raw)

    def funding_rate_history(self, symbol: str, start_time: int | None = None,
                             end_time: int | None = None, limit: int = 1000) -> list[dict]:
        return self._request(
            "GET", "/fapi/v1/fundingRate",
            {"symbol": symbol, "startTime": start_time, "endTime": end_time,
             "limit": min(limit, 1000)},
        )

    def exchange_info(self) -> dict:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    # --- аккаунт ----------------------------------------------------------

    def account(self) -> dict:
        return self._request("GET", "/fapi/v3/account", signed=True)

    def balance_usdt(self) -> float:
        account = self.account()
        for asset in account.get("assets", []):
            if asset.get("asset") == "USDT":
                return float(asset.get("availableBalance", 0))
        return 0.0

    def commission_rate(self, symbol: str) -> dict:
        return self._request(
            "GET", "/fapi/v1/commissionRate", {"symbol": symbol}, signed=True
        )

    def income_history(self, symbol: str | None = None, income_type: str | None = None,
                       start_time: int | None = None, limit: int = 100) -> list[dict]:
        return self._request(
            "GET", "/fapi/v1/income",
            {"symbol": symbol, "incomeType": income_type,
             "startTime": start_time, "limit": limit},
            signed=True,
        )

    # --- настройки позиции -------------------------------------------------

    def position_mode(self) -> bool:
        """True, если включён Hedge Mode."""
        payload = self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return bool(payload.get("dualSidePosition"))

    def change_leverage(self, symbol: str, leverage: int) -> dict:
        return self._request(
            "POST", "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": leverage}, signed=True,
        )

    def change_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        try:
            return self._request(
                "POST", "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": margin_type}, signed=True,
            )
        except BinanceAPIError as exc:
            if exc.code == -4046:  # "No need to change margin type" — уже установлен
                return {"code": -4046, "msg": "already set"}
            raise

    def position_risk(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        payload = self._request("GET", "/fapi/v3/positionRisk", params, signed=True)
        return payload if isinstance(payload, list) else [payload]

    # --- ордера -------------------------------------------------------------

    def new_market_order(self, symbol: str, side: str, quantity: float,
                         reduce_only: bool = False,
                         client_order_id: str | None = None) -> dict:
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "newOrderRespType": "RESULT",
            "newClientOrderId": client_order_id,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def get_order(self, symbol: str, client_order_id: str) -> dict:
        return self._request(
            "GET", "/fapi/v1/order",
            {"symbol": symbol, "origClientOrderId": client_order_id}, signed=True,
        )

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        return self._request(
            "GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True
        )

    def cancel_all_orders(self, symbol: str) -> dict:
        return self._request(
            "DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True
        )

    # Условные (SL/TP) ордера — Algo Service, обязательный с 2025-12-09
    def new_algo_order(self, symbol: str, side: str, order_type: str,
                       trigger_price: float, close_position: bool = True,
                       working_type: str = "MARK_PRICE",
                       client_algo_id: str | None = None) -> dict:
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": order_type,  # STOP_MARKET | TAKE_PROFIT_MARKET
            "triggerPrice": trigger_price,
            "workingType": working_type,
            "priceProtect": "true",
            "clientAlgoId": client_algo_id,
            "newOrderRespType": "RESULT",
        }
        if close_position:
            params["closePosition"] = "true"
        return self._request("POST", "/fapi/v1/algoOrder", params, signed=True)

    def open_algo_orders(self, symbol: str | None = None) -> list[dict]:
        payload = self._request(
            "GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True
        )
        if isinstance(payload, dict):
            return payload.get("orders", [])
        return payload

    def cancel_algo_order(self, symbol: str, algo_id: int | None = None,
                          client_algo_id: str | None = None) -> dict:
        return self._request(
            "DELETE", "/fapi/v1/algoOrder",
            {"symbol": symbol, "algoId": algo_id, "clientAlgoId": client_algo_id},
            signed=True,
        )


def klines_to_dataframe(raw: list) -> pd.DataFrame:
    columns = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ]
    df = pd.DataFrame(raw, columns=columns)
    numeric = ["open", "high", "low", "close", "volume", "quote_volume"]
    df[numeric] = df[numeric].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"].astype("int64"), unit="ms", utc=True)
    return df
