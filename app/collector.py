"""Polls the mihomo /connections API and turns cumulative counters into deltas."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from typing import Any

SECRET_RE = re.compile(r'secret\\?"?\s*:\s*\\?"?([A-Za-z0-9_\-+/=.]{8,128})')
DIRECT = "DIRECT"


def _basename(path: str) -> str:
    if not path:
        return ""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


class ApiError(RuntimeError):
    pass


class Collector:
    """Single background thread: fetch -> diff -> store."""

    def __init__(self, cfg, store) -> None:
        self.cfg = cfg
        self.store = store
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="collector", daemon=True)

        self._last: dict[str, tuple[int, int]] = {}
        self._meta: dict[str, tuple[str, str, str, str]] = {}
        self._global: tuple[int, int] = (0, 0)
        self._primed = False
        self.device = getattr(cfg, "device_name", "") or "local"
        self._rates: deque[tuple[float, int, int]] = deque(maxlen=300)
        self._live: list[dict[str, Any]] = []
        self._event_ts: dict[str, float] = {}

        self.upload_total = 0
        self.download_total = 0
        self.connections = 0
        self.ok = False
        self.last_ok = 0.0
        self.last_error = ""
        self.started = time.time()
        self.secret_source = "none"

        self._token = ""
        self._secret_mtime = -1.0

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------- secret
    def _candidates(self) -> list[str]:
        """CLASH_SECRET_FILE may point at one file or at a whole config directory."""
        path = self.cfg.secret_file
        if not path:
            return []
        if os.path.isdir(path):
            names = set(os.listdir(path))
            # priority order matters: pick the file most likely to hold the secret
            wanted = ("shared_preferences.json", "config.yaml", "clash-verge.yaml", "verge.yaml")
            preferred = [n for n in wanted if n in names]
            rest = sorted(n for n in names
                          if n not in preferred and n.endswith((".yaml", ".yml", ".json")))
            return [os.path.join(path, n) for n in preferred + rest]
        return [path] if os.path.exists(path) else []

    def _secret(self, force: bool = False) -> str:
        if self.cfg.secret:
            self.secret_source = "env"
            return self.cfg.secret
        for path in self._candidates():
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if not force and self._token and mtime == self._secret_mtime:
                return self._token
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                    raw = handle.read()
            except OSError:
                continue
            match = SECRET_RE.search(raw)
            if match:
                self._token = match.group(1)
                self._secret_mtime = mtime
                self.secret_source = "file"
                return self._token
        return self._token

    # -------------------------------------------------------------------- fetch
    def _get(self, token: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self.cfg.clash_api + "/connections",
            headers={"Accept": "application/json", "User-Agent": "clash-radar/1.0"},
        )
        if token:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise ApiError(f"HTTP {exc.code} from {self.cfg.clash_api}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(str(getattr(exc, "reason", exc))) from exc
        except ValueError as exc:
            raise ApiError(f"bad json: {exc}") from exc

    def _fetch(self) -> dict[str, Any]:
        token = self._secret()
        try:
            return self._get(token)
        except ApiError as exc:
            if "HTTP 401" not in str(exc):
                raise
            token = self._secret(force=True)
            self._event("auth_refresh", "controller rejected token, re-read secret file")
            return self._get(token)

    # ------------------------------------------------------------------- ingest
    def _ingest(self, payload: dict[str, Any], elapsed: float) -> None:
        minute = int(time.time() // 60 * 60)
        connections = payload.get("connections") or []
        rows: dict[tuple[int, str, str, str, str, str], list[int]] = defaultdict(lambda: [0, 0])
        seen: set[str] = set()
        live: list[dict[str, Any]] = []
        up_delta = down_delta = 0
        reset_seen = False

        for conn in connections:
            cid = str(conn.get("id") or "")
            if not cid:
                continue
            meta = conn.get("metadata") or {}
            up = int(conn.get("upload") or 0)
            down = int(conn.get("download") or 0)
            previous = self._last.get(cid)
            if previous is None:
                delta_up, delta_down = up, down
            else:
                delta_up, delta_down = up - previous[0], down - previous[1]
                if delta_up < 0 or delta_down < 0:
                    # core restarted or the id got reused: baselines are meaningless
                    self._event("core_restart", "connection counters went backwards; baseline reset")
                    delta_up = delta_down = 0
                    reset_seen = True
            self._last[cid] = (up, down)
            seen.add(cid)

            chains = [str(x) for x in (conn.get("chains") or []) if x]
            node = chains[0] if chains else DIRECT
            rule = str(conn.get("rule") or "")
            payload_rule = str(conn.get("rulePayload") or "")
            rule_label = f"{rule},{payload_rule}" if payload_rule else rule
            process = _basename(str(meta.get("process") or "")) or "(unknown)"
            host = str(meta.get("host") or "") or str(meta.get("destinationIP") or "") or "(unknown)"
            self._meta[cid] = (process, node, host, rule_label)

            if delta_up or delta_down:
                row = rows[(minute, self.device, process, node, host, rule_label)]
                row[0] += delta_up
                row[1] += delta_down
                up_delta += delta_up
                down_delta += delta_down

            live.append(
                {
                    "id": cid,
                    "process": process,
                    "host": host,
                    "port": meta.get("destinationPort"),
                    "node": node,
                    "rule": rule_label,
                    "chains": chains,
                    "up": up,
                    "down": down,
                    "start": conn.get("start"),
                }
            )

        vanished: list[tuple[tuple[str, str, str, str], int]] = []
        for cid in [c for c in self._last if c not in seen]:
            dimensions = self._meta.pop(cid, None)
            last = self._last.pop(cid, (0, 0))
            if dimensions:
                vanished.append((dimensions, last[0] + last[1]))

        live.sort(key=lambda item: item["up"] + item["down"], reverse=True)
        self._live = live[:300]
        self.connections = len(connections)

        now = time.time()

        # First poll only establishes baselines.  Without this, every container
        # restart would re-add the full lifetime counters of all live
        # connections (a long-lived browser socket can be hundreds of MB).
        global_up = int(payload.get("uploadTotal") or 0)
        global_down = int(payload.get("downloadTotal") or 0)
        if not self._primed:
            self._primed = True
            self._global = (global_up, global_down)
            return

        global_up_delta = max(0, global_up - self._global[0])
        global_down_delta = max(0, global_down - self._global[1])
        self._global = (global_up, global_down)

        # Bytes belonging to connections that closed between two polls never
        # show up in a per-connection delta.  The global counter knows about
        # them, so attribute that residual to the connections that just died.
        if not reset_seen:
            residual_up = global_up_delta - up_delta
            residual_down = global_down_delta - down_delta
            if residual_up > 0 or residual_down > 0:
                self._attribute_residual(
                    rows, minute, vanished, max(0, residual_up), max(0, residual_down)
                )
                up_delta += max(0, residual_up)
                down_delta += max(0, residual_down)

        if rows:
            self.store.add_usage(
                [(key[0], key[1], key[2], key[3], key[4], key[5], val[0], val[1])
                 for key, val in rows.items()]
            )

        self.upload_total += up_delta
        self.download_total += down_delta
        self._rates.append((now, up_delta, down_delta))
        while self._rates and now - self._rates[0][0] > 120:
            self._rates.popleft()
        if elapsed <= 0:
            elapsed = self.cfg.poll_interval
        self.last_rate = (up_delta / elapsed, down_delta / elapsed)

    def _attribute_residual(self, rows, minute, vanished, res_up, res_down) -> None:
        if not vanished:
            # bytes we cannot tie to any connection (very short-lived flows)
            row = rows[(minute, self.device, "(unattributed)", "(unattributed)", "(closed)", "(closed)")]
            row[0] += res_up
            row[1] += res_down
            return
        total_weight = sum(weight for _, weight in vanished) or 1
        remaining_up, remaining_down = res_up, res_down
        for index, (dimensions, weight) in enumerate(vanished):
            process, node, host, rule = dimensions
            row = rows[(minute, self.device, process, node, host, rule)]
            if index == len(vanished) - 1:
                share_up, share_down = remaining_up, remaining_down
            else:
                share_up = int(res_up * weight / total_weight)
                share_down = int(res_down * weight / total_weight)
                remaining_up -= share_up
                remaining_down -= share_down
            row[0] += share_up
            row[1] += share_down

    # --------------------------------------------------------------------- loop
    def _event(self, kind: str, detail: str) -> None:
        now = time.time()
        if now - self._event_ts.get(kind, 0) < 30:
            return
        self._event_ts[kind] = now
        try:
            self.store.add_event(kind, detail)
        except Exception:  # never let logging kill the collector
            pass

    def _run(self) -> None:
        backoff = 1.0
        last_cleanup = 0.0
        while not self._stop.is_set():
            started = time.time()
            try:
                payload = self._fetch()
                self._ingest(payload, time.time() - started)
                self.ok = True
                self.last_ok = time.time()
                self.last_error = ""
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001 - collector must survive anything
                self.ok = False
                self.last_error = str(exc)[:300]
                self._event("api_error", self.last_error)
                self._stop.wait(min(backoff, 30.0))
                backoff = min(backoff * 2, 30.0)
                continue

            if time.time() - last_cleanup > 3600:
                last_cleanup = time.time()
                try:
                    self.store.cleanup(self.cfg.detail_days)
                except Exception:
                    pass

            spent = time.time() - started
            self._stop.wait(max(0.05, self.cfg.poll_interval - spent))

    # ------------------------------------------------------------------- reads
    def rate(self, window: float = 5.0) -> tuple[float, float]:
        now = time.time()
        points = [p for p in self._rates if now - p[0] <= window]
        if len(points) < 2:
            points = list(self._rates)[-2:]
        if len(points) < 2:
            return 0.0, 0.0
        span = max(points[-1][0] - points[0][0], 0.001)
        up = sum(p[1] for p in points[1:]) / span
        down = sum(p[2] for p in points[1:]) / span
        return up, down

    def live(self) -> list[dict[str, Any]]:
        return self._live

    def status(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "last_ok": int(self.last_ok),
            "last_error": self.last_error,
            "connections": self.connections,
            "poll_interval": self.cfg.poll_interval,
            "secret": self.secret_source,
            "api": self.cfg.clash_api,
            "uptime": int(time.time() - self.started),
        }
