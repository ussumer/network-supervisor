"""SQLite storage: minute-level usage buckets, node multipliers, settings, events.

Design notes
------------
* Every row is keyed by (minute, process, node, host, rule) and stores *incremental*
  bytes, never cumulative counters.  The collector diffs the API for us.
* ``node`` is the first hop of the connection chain (the actual outbound node).
  ``DIRECT`` / ``REJECT`` rows are kept for visibility but never billed.
* Multipliers live in their own table and are applied at read time, so editing a
  ratio immediately re-computes history instead of corrupting stored numbers.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_min (
    minute  INTEGER NOT NULL,
    process TEXT    NOT NULL,
    node    TEXT    NOT NULL,
    host    TEXT    NOT NULL,
    rule    TEXT    NOT NULL,
    up      INTEGER NOT NULL DEFAULT 0,
    down    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (minute, process, node, host, rule)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS node_ratio (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT    NOT NULL UNIQUE,
    ratio   REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
    ts     INTEGER NOT NULL,
    kind   TEXT    NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_ts ON event(ts);
"""

DIRECT_NODES = {"DIRECT", "REJECT", ""}
DIMS = {"process", "host", "node", "rule"}


class Store:
    def __init__(self, path: str) -> None:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=30000")
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()
        self._ratios: list[tuple[re.Pattern[str], float]] = []
        self._ratios_ts = 0.0

    # ------------------------------------------------------------------ writes
    def add_usage(self, rows: Iterable[tuple[int, str, str, str, str, int, int]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self._db.executemany(
                """INSERT INTO usage_min (minute, process, node, host, rule, up, down)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(minute, process, node, host, rule)
                   DO UPDATE SET up = up + excluded.up, down = down + excluded.down""",
                rows,
            )
            self._db.commit()
        return len(rows)

    def add_event(self, kind: str, detail: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO event (ts, kind, detail) VALUES (?, ?, ?)",
                (int(time.time()), kind, detail[:400]),
            )
            self._db.commit()

    def cleanup(self, days: int) -> int:
        cutoff = int(time.time()) - max(1, days) * 86400
        with self._lock:
            cur = self._db.execute("DELETE FROM usage_min WHERE minute < ?", (cutoff,))
            self._db.commit()
        return cur.rowcount

    # --------------------------------------------------------------- settings
    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM settings").fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                out[row["key"]] = row["value"]
        return out

    def put_settings(self, values: dict[str, Any]) -> None:
        if not values:
            return
        with self._lock:
            self._db.executemany(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                [(k, json.dumps(v, ensure_ascii=False)) for k, v in values.items()],
            )
            self._db.commit()

    # ---------------------------------------------------------------- ratios
    def load_ratios(self, force: bool = False) -> list[tuple[re.Pattern[str], float]]:
        now = time.time()
        if not force and self._ratios and now - self._ratios_ts < 5:
            return self._ratios
        with self._lock:
            rows = self._db.execute(
                "SELECT pattern, ratio FROM node_ratio ORDER BY seq"
            ).fetchall()
        compiled: list[tuple[re.Pattern[str], float]] = []
        for row in rows:
            try:
                compiled.append((re.compile(row["pattern"] or ".*"), float(row["ratio"])))
            except (re.error, TypeError, ValueError):
                continue
        self._ratios = compiled
        self._ratios_ts = now
        return compiled

    def set_ratios(self, items: list[dict[str, Any]]) -> None:
        rows: list[tuple[str, float]] = []
        for item in items or []:
            pattern = str(item.get("pattern") or "").strip()
            if not pattern:
                continue
            try:
                re.compile(pattern)
                ratio = float(item.get("ratio"))
            except (re.error, TypeError, ValueError):
                continue
            if ratio <= 0:
                continue
            rows.append((pattern, ratio))
        with self._lock:
            self._db.execute("DELETE FROM node_ratio")
            self._db.executemany("INSERT INTO node_ratio (pattern, ratio) VALUES (?, ?)", rows)
            self._db.commit()
        self.load_ratios(force=True)

    def ratio_for(self, node: str, ratios: list[tuple[re.Pattern[str], float]] | None = None) -> float:
        ratios = ratios if ratios is not None else self.load_ratios()
        for pattern, ratio in ratios:
            if pattern.search(node):
                return ratio
        return 1.0

    # ------------------------------------------------------------- aggregates
    def by_node(self, start: int, end: int) -> dict[str, dict[str, int]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT node, SUM(up) AS u, SUM(down) AS d
                   FROM usage_min WHERE minute >= ? AND minute < ?
                   GROUP BY node""",
                (start, end),
            ).fetchall()
        return {
            (row["node"] or ""): {"up": int(row["u"] or 0), "down": int(row["d"] or 0)}
            for row in rows
        }

    def totals(self, start: int, end: int, billing_mode: str = "updown") -> dict[str, Any]:
        nodes = self.by_node(start, end)
        ratios = self.load_ratios()
        out: dict[str, Any] = {
            "up": 0, "down": 0, "direct": 0, "raw": 0, "billed": 0.0, "nodes": {},
        }
        for node, value in nodes.items():
            up, down = value["up"], value["down"]
            total = up + down
            out["up"] += up
            out["down"] += down
            if node in DIRECT_NODES:
                out["direct"] += total
                continue
            raw = down if billing_mode == "down" else total
            ratio = self.ratio_for(node, ratios)
            billed = raw * ratio
            out["raw"] += raw
            out["billed"] += billed
            out["nodes"][node] = {"up": up, "down": down, "ratio": ratio, "billed": billed}
        out["billed"] = int(round(out["billed"]))
        return out

    def top(self, start: int, end: int, dim: str, limit: int = 10,
            billing_mode: str = "updown") -> list[dict[str, Any]]:
        if dim not in DIMS:
            raise ValueError(f"bad dimension: {dim}")
        with self._lock:
            rows = self._db.execute(
                f"""SELECT {dim} AS k, node, SUM(up) AS u, SUM(down) AS d
                    FROM usage_min WHERE minute >= ? AND minute < ?
                    GROUP BY {dim}, node""",
                (start, end),
            ).fetchall()
        ratios = self.load_ratios()
        agg: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = row["k"] or "(unknown)"
            node = row["node"] or ""
            up, down = int(row["u"] or 0), int(row["d"] or 0)
            item = agg.setdefault(key, {"name": key, "up": 0, "down": 0, "direct": 0,
                                        "raw": 0, "billed": 0.0, "ratio": 1.0})
            if dim == "node":
                item["ratio"] = self.ratio_for(node, ratios)
            item["up"] += up
            item["down"] += down
            if node in DIRECT_NODES:
                item["direct"] += up + down
                continue
            raw = down if billing_mode == "down" else up + down
            item["raw"] += raw
            item["billed"] += raw * self.ratio_for(node, ratios)
        items = sorted(agg.values(), key=lambda i: i["billed"] or i["direct"], reverse=True)[:limit]
        peak = max([i["billed"] + i["direct"] for i in items] or [1]) or 1
        for item in items:
            item["billed"] = int(round(item["billed"]))
            item["share"] = round((item["billed"] + item["direct"]) / peak * 100, 1)
        return items

    def series(self, start: int, end: int, step: int = 60,
               billing_mode: str = "updown") -> list[dict[str, int]]:
        step = max(60, int(step))
        with self._lock:
            rows = self._db.execute(
                """SELECT (minute / ?) * ? AS bucket, node, SUM(up) AS u, SUM(down) AS d
                   FROM usage_min WHERE minute >= ? AND minute < ?
                   GROUP BY bucket, node ORDER BY bucket""",
                (step, step, start, end),
            ).fetchall()
        ratios = self.load_ratios()
        buckets: dict[int, dict[str, int]] = {}
        for row in rows:
            bucket = int(row["bucket"])
            point = buckets.setdefault(bucket, {"t": bucket, "up": 0, "down": 0, "billed": 0})
            up, down = int(row["u"] or 0), int(row["d"] or 0)
            point["up"] += up
            point["down"] += down
            node = row["node"] or ""
            if node in DIRECT_NODES:
                continue
            raw = down if billing_mode == "down" else up + down
            point["billed"] += int(round(raw * self.ratio_for(node, ratios)))
        return [buckets[k] for k in sorted(buckets)]

    def events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, kind, detail FROM event ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"ts": row["ts"], "kind": row["kind"], "detail": row["detail"]} for row in rows]

    def first_minute(self, start: int) -> int | None:
        """Earliest bucket we have recorded at or after ``start`` (None if empty)."""
        with self._lock:
            row = self._db.execute(
                "SELECT MIN(minute) AS m FROM usage_min WHERE minute >= ?", (start,)
            ).fetchone()
        if not row or row["m"] is None:
            return None
        return int(row["m"])

    def stats(self) -> dict[str, Any]:
        with self._lock:
            count = self._db.execute("SELECT COUNT(*) AS c FROM usage_min").fetchone()["c"]
        size = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                size += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return {"rows": int(count), "db_bytes": size, "path": self.path}

    def close(self) -> None:
        with self._lock:
            try:
                self._db.commit()
                self._db.close()
            except sqlite3.Error:
                pass
