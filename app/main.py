"""HTTP server + JSON API + accounting math (standard library only)."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .collector import Collector
from .config import Config
from .store import Store

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}
BILLING_MODES = {"updown", "down"}
CYCLE_MODES = {"calendar", "rolling"}


def local_midnight(ts: int, offset_hours: float) -> tuple[int, int]:
    tz = timezone(timedelta(hours=offset_hours))
    start = datetime.fromtimestamp(ts, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def cycle_window(ts: int, offset_hours: float, mode: str, start_day: int,
                 days: int) -> tuple[int, int]:
    if mode == "rolling":
        return ts - max(1, days) * 86400, ts
    tz = timezone(timedelta(hours=offset_hours))
    now = datetime.fromtimestamp(ts, tz)
    day = min(max(1, int(start_day)), 28)
    start = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if start > now:
        year, month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        start = datetime(year, month, day, tzinfo=tz)
    year, month = (start.year, start.month + 1) if start.month < 12 else (start.year + 1, 1)
    end = datetime(year, month, day, tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class App:
    def __init__(self, cfg: Config, store: Store, collector: Collector) -> None:
        self.cfg = cfg
        self.store = store
        self.collector = collector
        defaults = cfg.defaults()
        stored = store.get_settings()
        self.settings = {**defaults, **stored}
        if not stored:
            store.put_settings(defaults)
        self.settings["billing_mode"] = str(self.settings.get("billing_mode") or "updown").lower()
        if self.settings["billing_mode"] not in BILLING_MODES:
            self.settings["billing_mode"] = "updown"

    # -------------------------------------------------------------- accounting
    @property
    def billing_mode(self) -> str:
        return self.settings.get("billing_mode", "updown")

    def overview(self) -> dict:
        now = int(time.time())
        today_start, today_end = local_midnight(now, self.cfg.tz_offset)
        start, end = cycle_window(
            now,
            self.cfg.tz_offset,
            self.settings.get("cycle_mode", "calendar"),
            int(self.settings.get("cycle_start_day", 1)),
            int(self.settings.get("cycle_days", 30)),
        )
        today = self.store.totals(today_start, today_end, self.billing_mode)
        cycle = self.store.totals(start, end, self.billing_mode)
        budget = float(self.settings.get("budget_gb", self.cfg.budget_gb)) * 1024 ** 3
        # average over the window we actually observed, not the whole billing cycle
        first = self.store.first_minute(start)
        observed_start = max(start, first) if first else now
        observed_secs = max(now - observed_start, 300)
        elapsed_days = observed_secs / 86400.0
        avg_day = cycle["billed"] / elapsed_days
        remaining = max(budget - cycle["billed"], 0)
        up_rate, down_rate = self.collector.rate()
        # projecting from a cold start is meaningless; wait for an hour of data
        can_project = observed_secs >= 3600 and avg_day > 0
        return {
            "now": now,
            "collector": self.collector.status(),
            "rates": {"up": round(up_rate, 1), "down": round(down_rate, 1)},
            "today": today,
            "cycle": {
                "start": start,
                "end": end,
                "budget": int(budget),
                "used": cycle["billed"],
                "pct": round(_clamp(cycle["billed"] / budget * 100 if budget else 0, 0, 999), 2),
                "remaining": int(remaining),
                "avg_day": int(avg_day),
                "elapsed_days": round(elapsed_days, 2),
                "days_left": round(remaining / avg_day, 1) if can_project else None,
                "raw": cycle["raw"],
                "direct": cycle["direct"],
                "up": cycle["up"],
                "down": cycle["down"],
            },
            "settings": self.public_settings(),
            "session": {
                "up": self.collector.upload_total,
                "down": self.collector.download_total,
            },
        }

    def public_settings(self) -> dict:
        return {
            "budget_gb": float(self.settings.get("budget_gb", self.cfg.budget_gb)),
            "billing_mode": self.billing_mode,
            "cycle_mode": self.settings.get("cycle_mode", "calendar"),
            "cycle_start_day": int(self.settings.get("cycle_start_day", 1)),
            "cycle_days": int(self.settings.get("cycle_days", 30)),
            "ratios": [
                {"pattern": rx.pattern, "ratio": ratio}
                for rx, ratio in self.store.load_ratios()
            ],
        }

    # ----------------------------------------------------------------- updates
    def update_settings(self, body: dict) -> dict:
        changed: dict = {}
        if "budget_gb" in body:
            try:
                value = float(body["budget_gb"])
                if value > 0:
                    changed["budget_gb"] = value
            except (TypeError, ValueError):
                pass
        if "billing_mode" in body and str(body["billing_mode"]) in BILLING_MODES:
            changed["billing_mode"] = str(body["billing_mode"])
        if "cycle_mode" in body and str(body["cycle_mode"]) in CYCLE_MODES:
            changed["cycle_mode"] = str(body["cycle_mode"])
        if "cycle_start_day" in body:
            try:
                changed["cycle_start_day"] = int(_clamp(float(body["cycle_start_day"]), 1, 28))
            except (TypeError, ValueError):
                pass
        if "cycle_days" in body:
            try:
                changed["cycle_days"] = int(_clamp(float(body["cycle_days"]), 1, 365))
            except (TypeError, ValueError):
                pass
        if changed:
            self.settings.update(changed)
            self.store.put_settings(changed)
        if isinstance(body.get("ratios"), list):
            self.store.set_ratios(body["ratios"])
        return self.public_settings()

    # -------------------------------------------------------------------- data
    def series(self, minutes: int) -> dict:
        minutes = int(_clamp(minutes, 5, 60 * 24 * 90))
        now = int(time.time())
        end = now - now % 60
        start = end - minutes * 60
        step = 60 if minutes <= 180 else 300 if minutes <= 1440 else 1800
        points = self.store.series(start, end, step, self.billing_mode)
        index = {p["t"]: p for p in points}
        filled = []
        bucket = start - start % step
        while bucket <= end:
            point = index.get(bucket)
            filled.append(point or {"t": bucket, "up": 0, "down": 0, "billed": 0})
            bucket += step
        return {"step": step, "points": filled}

    def top(self, dim: str, hours: float, limit: int) -> dict:
        limit = int(_clamp(limit, 1, 50))
        now = int(time.time())
        start = now - int(_clamp(hours, 0.25, 24 * 90) * 3600)
        return {"dim": dim, "items": self.store.top(start, now, dim, limit, self.billing_mode)}

    def live(self, limit: int) -> dict:
        limit = int(_clamp(limit, 1, 300))
        up_rate, down_rate = self.collector.rate()
        return {
            "connections": self.collector.live()[:limit],
            "rates": {"up": round(up_rate, 1), "down": round(down_rate, 1)},
            "status": self.collector.status(),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "clash-radar/1.0"
    app: App  # injected by make_server

    # ------------------------------------------------------------------ helpers
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _file(self, name: str) -> None:
        path = os.path.normpath(os.path.join(STATIC_DIR, name))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as handle:
            body = handle.read()
        self._send(200, body, CONTENT_TYPES.get(ext, "application/octet-stream"))

    def log_message(self, fmt: str, *args) -> None:  # keep container logs clean
        return

    # --------------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        def q(name: str, default: str) -> str:
            return (query.get(name) or [default])[0]

        try:
            if path in ("/", "/index.html"):
                self._file("index.html")
            elif path.startswith("/static/"):
                self._file(path[len("/static/"):])
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            elif path == "/api/health":
                self._json({"ok": True, "collector": self.app.collector.ok})
            elif path == "/api/overview":
                self._json(self.app.overview())
            elif path == "/api/series":
                self._json(self.app.series(int(q("minutes", "60"))))
            elif path == "/api/top":
                self._json(self.app.top(q("dim", "process"), float(q("hours", "24")),
                                        int(q("limit", "10"))))
            elif path == "/api/live":
                self._json(self.app.live(int(q("limit", "50"))))
            elif path == "/api/events":
                self._json({"items": self.app.store.events(int(q("limit", "20")))})
            elif path == "/api/settings":
                self._json(self.app.public_settings())
            elif path == "/api/meta":
                self._json({
                    "store": self.app.store.stats(),
                    "config": {
                        "api": self.app.cfg.clash_api,
                        "db": self.app.cfg.db_path,
                        "poll": self.app.cfg.poll_interval,
                        "tz_offset": self.app.cfg.tz_offset,
                        "retention_days": self.app.cfg.detail_days,
                    },
                    "settings": self.app.public_settings(),
                })
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)

    # -------------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            if parsed.path == "/api/settings":
                self._json(self.app.update_settings(body if isinstance(body, dict) else {}))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 400)


def make_server(app: App) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((app.cfg.listen_host, app.cfg.port), handler)
    server.daemon_threads = True
    return server


def main() -> int:
    cfg = Config()
    store = Store(cfg.db_path)
    collector = Collector(cfg, store)
    app = App(cfg, store, collector)
    collector.start()
    server = make_server(app)

    def _shutdown(signum, frame) -> None:  # noqa: ARG001
        collector.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"clash-radar listening on :{cfg.port}  api={cfg.clash_api}  db={cfg.db_path}", flush=True)
    try:
        server.serve_forever()
    finally:
        collector.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
