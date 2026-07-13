from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .strategy import Signal


class Journal:
    """SQLite-журнал сигналов, сделок и состояния engine (потокобезопасный)."""

    def __init__(self, path: str = "data/trading.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    candle_time TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry REAL,
                    stop REAL,
                    take_profit REAL,
                    score INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    UNIQUE(symbol, candle_time, side)
                );
                CREATE TABLE IF NOT EXISTS engine_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    day TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    qty REAL NOT NULL,
                    entry REAL,
                    stop REAL,
                    take_profit REAL,
                    status TEXT NOT NULL,
                    realized_pnl REAL
                );
                CREATE TABLE IF NOT EXISTS engine_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                );
            """)
            self.conn.commit()

    def save_signal(self, signal: Signal) -> bool:
        with self._lock:
            try:
                self.conn.execute(
                    """
                    INSERT INTO signals
                    (candle_time, symbol, side, entry, stop, take_profit, score, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal.candle_time, signal.symbol, signal.side.value,
                        signal.entry, signal.stop, signal.take_profit,
                        signal.score, signal.reason,
                    ),
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # --- engine ---------------------------------------------------------

    def record_trade_open(self, day: str, symbol: str, side: str,
                          client_order_id: str, qty: float, entry: float | None,
                          stop: float, take_profit: float) -> bool:
        """False, если такой client_order_id уже есть (идемпотентность)."""
        with self._lock:
            try:
                self.conn.execute(
                    """
                    INSERT INTO engine_trades
                    (day, symbol, side, client_order_id, qty, entry, stop, take_profit, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
                    """,
                    (day, symbol, side, client_order_id, qty, entry, stop, take_profit),
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def has_trade(self, client_order_id: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM engine_trades WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return row is not None

    def close_trade(self, client_order_id: str, status: str,
                    realized_pnl: float | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE engine_trades SET status = ?, realized_pnl = ? "
                "WHERE client_order_id = ?",
                (status, realized_pnl, client_order_id),
            )
            self.conn.commit()

    def open_trades(self, symbol: str | None = None) -> list[dict]:
        query = "SELECT * FROM engine_trades WHERE status = 'OPEN'"
        args: tuple = ()
        if symbol:
            query += " AND symbol = ?"
            args = (symbol,)
        with self._lock:
            cur = self.conn.execute(query, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def closed_trades(self, limit: int | None = None) -> list[dict]:
        """Сделки с зафиксированным результатом, новые первыми."""
        query = ("SELECT * FROM engine_trades WHERE realized_pnl IS NOT NULL "
                 "ORDER BY id DESC")
        if limit:
            query += f" LIMIT {int(limit)}"
        with self._lock:
            cur = self.conn.execute(query)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def trades_on_day(self, day: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM engine_trades WHERE day = ?", (day,)
            ).fetchone()
        return int(row[0])

    def realized_pnl_on_day(self, day: str) -> float:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM engine_trades "
                "WHERE day = ? AND realized_pnl IS NOT NULL", (day,)
            ).fetchone()
        return float(row[0])

    def log_event(self, level: str, message: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO engine_events (level, message) VALUES (?, ?)",
                (level, message),
            )
            self.conn.commit()

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT created_at, level, message FROM engine_events "
                "ORDER BY id DESC LIMIT ?", (limit,)
            )
            return [
                {"created_at": r[0], "level": r[1], "message": r[2]}
                for r in cur.fetchall()
            ]
