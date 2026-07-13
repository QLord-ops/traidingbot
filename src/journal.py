from __future__ import annotations

import sqlite3
from pathlib import Path
from .strategy import Signal


class Journal:
    def __init__(self, path: str = "data/trading.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("""
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
            )
        """)
        self.conn.commit()

    def save_signal(self, signal: Signal) -> bool:
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
