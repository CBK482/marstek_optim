"""SQLite persistence (``/data/state.db``): samples, actions, plans, peaks."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL PRIMARY KEY, soc REAL, p1_w REAL, avg_demand_w REAL,
    batt_w REAL, house_w REAL, cmd TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mode TEXT, action TEXT,
    power_w REAL, source TEXT, dry_run INTEGER, reason TEXT
);
CREATE TABLE IF NOT EXISTS plans (ts REAL PRIMARY KEY, summary TEXT, plan TEXT);
CREATE TABLE IF NOT EXISTS peaks (month TEXT PRIMARY KEY, peak_w REAL, ts REAL);
"""


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = cur.fetchall()
            self._db.commit()
            return rows

    # ---- samples
    def add_sample(self, ts: datetime, soc, p1, avg, batt, house, cmd: str) -> None:
        self._exec("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?)",
                   (ts.timestamp(), soc, p1, avg, batt, house, cmd))

    def house_samples(self, since: datetime) -> list[tuple[datetime, float]]:
        rows = self._exec("SELECT ts, house_w FROM samples WHERE ts>=? AND house_w IS NOT NULL ORDER BY ts",
                          (since.timestamp(),))
        return [(datetime.fromtimestamp(t, tz=timezone.utc), h) for t, h in rows]

    def recent_samples(self, minutes: int = 180) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp()
        rows = self._exec("SELECT ts, soc, p1_w, avg_demand_w, batt_w, house_w, cmd FROM samples "
                          "WHERE ts>=? ORDER BY ts", (since,))
        keys = ("ts", "soc", "p1_w", "avg_demand_w", "batt_w", "house_w", "cmd")
        return [dict(zip(keys, r)) for r in rows]

    # ---- actions
    def add_action(self, mode: str, action: str, power: float, source: str, dry_run: bool, reason: str) -> None:
        self._exec("INSERT INTO actions (ts, mode, action, power_w, source, dry_run, reason) VALUES (?,?,?,?,?,?,?)",
                   (datetime.now(timezone.utc).timestamp(), mode, action, power, source, int(dry_run), reason))

    def actions(self, limit: int = 200) -> list[dict]:
        rows = self._exec("SELECT ts, mode, action, power_w, source, dry_run, reason FROM actions "
                          "ORDER BY id DESC LIMIT ?", (limit,))
        keys = ("ts", "mode", "action", "power_w", "source", "dry_run", "reason")
        return [dict(zip(keys, r)) for r in rows]

    # ---- plans
    def save_plan(self, summary: dict, plan: Any) -> None:
        self._exec("INSERT OR REPLACE INTO plans VALUES (?,?,?)",
                   (datetime.now(timezone.utc).timestamp(), json.dumps(summary), json.dumps(plan)))

    def last_plan(self) -> Optional[dict]:
        rows = self._exec("SELECT plan FROM plans ORDER BY ts DESC LIMIT 1")
        return json.loads(rows[0][0]) if rows else None

    # ---- monthly peak
    def update_peak(self, month: str, peak_w: float) -> float:
        rows = self._exec("SELECT peak_w FROM peaks WHERE month=?", (month,))
        cur = rows[0][0] if rows else 0.0
        if peak_w > cur:
            self._exec("INSERT OR REPLACE INTO peaks VALUES (?,?,?)",
                       (month, peak_w, datetime.now(timezone.utc).timestamp()))
            return peak_w
        return cur

    def get_peak(self, month: str) -> float:
        rows = self._exec("SELECT peak_w FROM peaks WHERE month=?", (month,))
        return float(rows[0][0]) if rows else 0.0

    def prune(self, days: int = 35) -> None:
        cut = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        self._exec("DELETE FROM samples WHERE ts<?", (cut,))
        self._exec("DELETE FROM actions WHERE ts<?", (cut,))
        self._exec("DELETE FROM plans WHERE ts<?", ((datetime.now(timezone.utc) - timedelta(days=2)).timestamp(),))
