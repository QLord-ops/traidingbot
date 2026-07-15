from __future__ import annotations

"""Календарь плановых макро-событий для фильтра входов.

Смысл фильтра: вокруг публикаций CPI и решений FOMC случаются резкие
двусторонние движения, прокалывающие стопы; направленного edge у breakout-
стратегии в эти минуты нет. Фильтр запрещает ОТКРЫВАТЬ новые позиции в окне
[event − before_h; event + after_h]; уже открытые позиции не трогаются —
их защищает обычный стоп.

Времена событий заданы в UTC с учётом летнего времени США:
- CPI (BLS): 08:30 ET = 12:30 UTC (лето) / 13:30 UTC (зима);
- FOMC (ФРС): 14:00 ET = 18:00 UTC (лето) / 19:00 UTC (зима).

Источники: federalreserve.gov/monetarypolicy/fomccalendars.htm,
bls.gov/schedule/news_release/cpi.htm (архив: bls.gov/bls/news-release/cpi.htm).
Публикация CPI 2025-11-13 отменена (шатдаун) — в списке отсутствует,
сентябрьский CPI 2025 вышел 24.10.2025.

ВНИМАНИЕ: календарь нужно продлевать раз в год (даты 2027+ отсутствуют).
"""

import pandas as pd

_RAW_EVENTS: list[tuple[str, str]] = [
    # --- CPI (публикация 08:30 ET) ---
    ("2023-07-12 12:30", "CPI"), ("2023-08-10 12:30", "CPI"),
    ("2023-09-13 12:30", "CPI"), ("2023-10-12 12:30", "CPI"),
    ("2023-11-14 13:30", "CPI"), ("2023-12-12 13:30", "CPI"),
    ("2024-01-11 13:30", "CPI"), ("2024-02-13 13:30", "CPI"),
    ("2024-03-12 12:30", "CPI"), ("2024-04-10 12:30", "CPI"),
    ("2024-05-15 12:30", "CPI"), ("2024-06-12 12:30", "CPI"),
    ("2024-07-11 12:30", "CPI"), ("2024-08-14 12:30", "CPI"),
    ("2024-09-11 12:30", "CPI"), ("2024-10-10 12:30", "CPI"),
    ("2024-11-13 13:30", "CPI"), ("2024-12-11 13:30", "CPI"),
    ("2025-01-15 13:30", "CPI"), ("2025-02-12 13:30", "CPI"),
    ("2025-03-12 12:30", "CPI"), ("2025-04-10 12:30", "CPI"),
    ("2025-05-13 12:30", "CPI"), ("2025-06-11 12:30", "CPI"),
    ("2025-07-15 12:30", "CPI"), ("2025-08-12 12:30", "CPI"),
    ("2025-09-11 12:30", "CPI"), ("2025-10-24 12:30", "CPI"),
    ("2025-12-18 13:30", "CPI"),
    ("2026-01-13 13:30", "CPI"), ("2026-02-13 13:30", "CPI"),
    ("2026-03-11 12:30", "CPI"), ("2026-04-10 12:30", "CPI"),
    ("2026-05-12 12:30", "CPI"), ("2026-06-10 12:30", "CPI"),
    ("2026-07-14 12:30", "CPI"), ("2026-08-12 12:30", "CPI"),
    ("2026-09-11 12:30", "CPI"), ("2026-10-14 12:30", "CPI"),
    ("2026-11-10 13:30", "CPI"), ("2026-12-10 13:30", "CPI"),
    # --- FOMC (решение 14:00 ET) ---
    ("2023-07-26 18:00", "FOMC"), ("2023-09-20 18:00", "FOMC"),
    ("2023-11-01 18:00", "FOMC"), ("2023-12-13 19:00", "FOMC"),
    ("2024-01-31 19:00", "FOMC"), ("2024-03-20 18:00", "FOMC"),
    ("2024-05-01 18:00", "FOMC"), ("2024-06-12 18:00", "FOMC"),
    ("2024-07-31 18:00", "FOMC"), ("2024-09-18 18:00", "FOMC"),
    ("2024-11-07 19:00", "FOMC"), ("2024-12-18 19:00", "FOMC"),
    ("2025-01-29 19:00", "FOMC"), ("2025-03-19 18:00", "FOMC"),
    ("2025-05-07 18:00", "FOMC"), ("2025-06-18 18:00", "FOMC"),
    ("2025-07-30 18:00", "FOMC"), ("2025-09-17 18:00", "FOMC"),
    ("2025-10-29 18:00", "FOMC"), ("2025-12-10 19:00", "FOMC"),
    ("2026-01-28 19:00", "FOMC"), ("2026-03-18 18:00", "FOMC"),
    ("2026-04-29 18:00", "FOMC"), ("2026-06-17 18:00", "FOMC"),
    ("2026-07-29 18:00", "FOMC"), ("2026-09-16 18:00", "FOMC"),
    ("2026-10-28 18:00", "FOMC"), ("2026-12-09 19:00", "FOMC"),
]

EVENTS: list[tuple[pd.Timestamp, str]] = [
    (pd.Timestamp(ts, tz="UTC"), label) for ts, label in _RAW_EVENTS
]
LAST_KNOWN_EVENT: pd.Timestamp = max(ts for ts, _ in EVENTS)


def in_blackout(ts, before_h: float, after_h: float,
                events: list[tuple[pd.Timestamp, str]] | None = None) -> str | None:
    """Метка события, если ts попадает в его blackout-окно, иначе None."""
    moment = pd.Timestamp(ts)
    if moment.tz is None:
        moment = moment.tz_localize("UTC")
    for event_ts, label in (events if events is not None else EVENTS):
        if event_ts - pd.Timedelta(hours=before_h) <= moment \
                <= event_ts + pd.Timedelta(hours=after_h):
            return f"{label} {event_ts:%Y-%m-%d %H:%M}"
    return None


def upcoming_events(now=None, days: int = 8) -> list[tuple[pd.Timestamp, str]]:
    """Плановые события в ближайшие N дней (для статуса и уведомлений)."""
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")
    horizon = moment + pd.Timedelta(days=days)
    return [(ts, label) for ts, label in EVENTS if moment <= ts <= horizon]


def calendar_is_stale(now=None, margin_days: int = 30) -> bool:
    """True, если календарь скоро закончится и его пора продлить."""
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")
    return moment > LAST_KNOWN_EVENT - pd.Timedelta(days=margin_days)
