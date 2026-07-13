from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from .binance_client import BinanceFuturesClient, INTERVAL_MS, KLINES_MAX_LIMIT

log = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]


def _cache_path(symbol: str, interval: str) -> Path:
    return CACHE_DIR / f"{symbol}_{interval}.csv"


def _load_cache(symbol: str, interval: str) -> pd.DataFrame | None:
    path = _cache_path(symbol, interval)
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["open_time", "close_time"])
        for col in ["open_time", "close_time"]:
            if df[col].dt.tz is None:
                df[col] = df[col].dt.tz_localize("UTC")
        return df
    except Exception as exc:  # повреждённый кэш не должен ронять backtest
        log.warning("Кэш %s повреждён (%s) — будет перезагружен", path, exc)
        return None


def _save_cache(df: pd.DataFrame, symbol: str, interval: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(_cache_path(symbol, interval), index=False)


def fetch_klines_range(client: BinanceFuturesClient, symbol: str, interval: str,
                       start_ms: int, end_ms: int) -> pd.DataFrame:
    """Загружает закрытые свечи [start_ms, end_ms) с пагинацией по 1500 штук."""
    if interval not in INTERVAL_MS:
        raise ValueError(f"Неподдерживаемый интервал: {interval}")
    step = INTERVAL_MS[interval]
    frames: list[pd.DataFrame] = []
    cursor = start_ms
    while cursor < end_ms:
        chunk = client.klines(
            symbol, interval, limit=KLINES_MAX_LIMIT,
            start_time=cursor, end_time=end_ms - 1,
        )
        if chunk.empty:
            break
        frames.append(chunk)
        last_open = int(chunk["open_time"].iloc[-1].timestamp() * 1000)
        next_cursor = last_open + step
        if next_cursor <= cursor:  # защита от зацикливания
            break
        cursor = next_cursor
        if len(chunk) < KLINES_MAX_LIMIT:
            break
    if not frames:
        return pd.DataFrame(columns=KLINE_COLUMNS)
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
    return df


def get_klines(client: BinanceFuturesClient, symbol: str, interval: str,
               start_ms: int, end_ms: int, use_cache: bool = True) -> pd.DataFrame:
    """Свечи за диапазон с локальным CSV-кэшем; докачиваются только дыры.

    Возвращаются только закрытые свечи (close_time < now), чтобы индикаторы
    никогда не считались по формирующейся свече.
    """
    now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    end_ms = min(end_ms, now_ms)
    step = INTERVAL_MS[interval]

    cached = _load_cache(symbol, interval) if use_cache else None
    if cached is not None and not cached.empty:
        have_start = int(cached["open_time"].iloc[0].timestamp() * 1000)
        have_end = int(cached["open_time"].iloc[-1].timestamp() * 1000) + step
        parts = [cached]
        if start_ms < have_start:
            parts.insert(0, fetch_klines_range(client, symbol, interval, start_ms, have_start))
        if end_ms > have_end:
            parts.append(fetch_klines_range(client, symbol, interval, have_end, end_ms))
        df = pd.concat(parts, ignore_index=True)
        df = df.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
    else:
        df = fetch_klines_range(client, symbol, interval, start_ms, end_ms)

    if use_cache and not df.empty:
        _save_cache(df, symbol, interval)

    mask = (
        (df["open_time"] >= pd.Timestamp(start_ms, unit="ms", tz="UTC"))
        & (df["open_time"] < pd.Timestamp(end_ms, unit="ms", tz="UTC"))
        & (df["close_time"] < pd.Timestamp(now_ms, unit="ms", tz="UTC"))
    )
    return df.loc[mask].reset_index(drop=True)


def get_funding_rates(client: BinanceFuturesClient, symbol: str,
                      start_ms: int, end_ms: int) -> pd.DataFrame:
    """История funding rate за диапазон (пагинация по 1000 записей)."""
    rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        chunk = client.funding_rate_history(symbol, start_time=cursor, end_time=end_ms, limit=1000)
        if not chunk:
            break
        rows.extend(chunk)
        last = int(chunk[-1]["fundingTime"])
        if last <= cursor:
            break
        cursor = last + 1
        if len(chunk) < 1000:
            break
    if not rows:
        return pd.DataFrame(columns=["funding_time", "funding_rate"])
    df = pd.DataFrame({
        "funding_time": pd.to_datetime([int(r["fundingTime"]) for r in rows], unit="ms", utc=True),
        "funding_rate": [float(r["fundingRate"]) for r in rows],
    })
    return df.drop_duplicates(subset="funding_time").sort_values("funding_time").reset_index(drop=True)
