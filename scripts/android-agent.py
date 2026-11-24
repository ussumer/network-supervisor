#!/usr/bin/env python3
"""clash-radar 手机端采集器 —— 在 Termux 里跑，把手机 Clash 内核的流量上报给 PC 看板。

用法（Termux）：
    pkg install -y python
    python android-agent.py --server http://192.168.1.10:8787 --token 你的上报口令

可选参数：
    --controller http://127.0.0.1:9090   手机里 Clash 内核的控制器地址
    --secret     xxx                     控制器 secret（客户端里没设就留空）
    --name       pixel-8                 设备名，看板里显示这个
    --interval   3                       轮询间隔（秒）
    --flush      30                      上报间隔（秒）
    --dry-run                            只打印，不上报

脚本只用标准库，Termux 装个 python 就能跑。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

DIRECT = "DIRECT"


def _basename(path: str) -> str:
    return (path or "").replace("\\", "/").rsplit("/", 1)[-1]


class PhoneAgent:
    def __init__(self, args) -> None:
        self.args = args
        self.device = args.name or socket.gethostname()
        self.last: dict[str, tuple[int, int]] = {}
        self.meta: dict[str, tuple[str, str, str, str]] = {}
        self.global_total = (0, 0)
        self.primed = False
        self.rows: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
        self.last_flush = time.time()
        self.sent_rows = 0
        self.failures = 0

    # ---------------------------------------------------------------- fetching
    def fetch(self) -> dict:
        url = self.args.controller.rstrip("/") + "/connections"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.args.secret:
            request.add_header("Authorization", "Bearer " + self.args.secret)
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    # ---------------------------------------------------------------- diffing
    def ingest(self, payload: dict) -> None:
        minute = int(time.time() // 60 * 60)
        connections = payload.get("connections") or []
        seen: set[str] = set()
        up_delta = down_delta = 0
        reset = False

        for conn in connections:
            cid = str(conn.get("id") or "")
            if not cid:
                continue
            meta = conn.get("metadata") or {}
            up = int(conn.get("upload") or 0)
            down = int(conn.get("download") or 0)
            previous = self.last.get(cid)
            if previous is None:
                d_up, d_down = up, down
            else:
                d_up, d_down = up - previous[0], down - previous[1]
                if d_up < 0 or d_down < 0:
                    d_up = d_down = 0
                    reset = True
            self.last[cid] = (up, down)
            seen.add(cid)

            chains = [str(x) for x in (conn.get("chains") or []) if x]
            node = chains[0] if chains else DIRECT
            rule = str(conn.get("rule") or "")
            payload_rule = str(conn.get("rulePayload") or "")
            rule_label = f"{rule},{payload_rule}" if payload_rule else rule
            process = _basename(str(meta.get("process") or "")) or "(unknown)"
            host = str(meta.get("host") or "") or str(meta.get("destinationIP") or "") or "(unknown)"
            self.meta[cid] = (process, node, host, rule_label)

            if d_up or d_down:
                row = self.rows[(minute, process, node, host, rule_label)]
                row[0] += d_up
                row[1] += d_down
                up_delta += d_up
                down_delta += d_down

        # 连接在两次轮询之间关闭时，用全局计数补差
        global_up = int(payload.get("uploadTotal") or 0)
        global_down = int(payload.get("downloadTotal") or 0)
        if not self.primed:
            self.primed = True
            self.global_total = (global_up, global_down)
            return

        g_delta_up = max(0, global_up - self.global_total[0])
        g_delta_down = max(0, global_down - self.global_total[1])
        self.global_total = (global_up, global_down)

        for cid in [c for c in self.last if c not in seen]:
            self.last.pop(cid, None)
            self.meta.pop(cid, None)

        if not reset:
            residual_up = g_delta_up - up_delta
            residual_down = g_delta_down - down_delta
            if residual_up > 0 or residual_down > 0:
                row = self.rows[(minute, "(unattributed)", "(unattributed)", "(closed)", "(closed)")]
                row[0] += max(0, residual_up)
                row[1] += max(0, residual_down)

    # ---------------------------------------------------------------- pushing
    def flush(self) -> None:
        if not self.rows:
            return
        payload = {
            "device": self.device,
            "rows": [
                {"minute": key[0], "process": key[1], "node": key[2],
                 "host": key[3], "rule": key[4], "up": val[0], "down": val[1]}
                for key, val in self.rows.items()
            ],
        }
        if self.args.dry_run:
            print(f"[dry-run] {len(payload['rows'])} rows", file=sys.stderr)
            self.rows.clear()
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.args.server.rstrip("/") + "/api/ingest",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.args.token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
            self.sent_rows += int(result.get("accepted") or 0)
            self.rows.clear()
            self.failures = 0
            self.last_flush = time.time()
            print(f"[ok] 已上报 {result.get('accepted')} 行，累计 {self.sent_rows}", file=sys.stderr)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.failures += 1
            print(f"[retry] 上报失败（{exc}），先攒着", file=sys.stderr)

    # ------------------------------------------------------------------- loop
    def run(self) -> int:
        print(f"设备 {self.device} → {self.args.server}", file=sys.stderr)
        print(f"控制器 {self.args.controller}，每 {self.args.interval}s 采一次", file=sys.stderr)
        while True:
            try:
                self.ingest(self.fetch())
            except urllib.error.HTTPError as exc:
                print(f"[error] 控制器返回 HTTP {exc.code}，检查 --secret", file=sys.stderr)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                print(f"[error] 连不上控制器：{exc}", file=sys.stderr)
            if time.time() - self.last_flush >= self.args.flush:
                self.flush()
            time.sleep(self.args.interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="把手机 Clash 流量上报到 clash-radar")
    parser.add_argument("--server", required=True, help="PC 看板地址，如 http://192.168.1.10:8787")
    parser.add_argument("--token", default=os.getenv("INGEST_TOKEN", ""), help="PC 端 INGEST_TOKEN")
    parser.add_argument("--controller", default="http://127.0.0.1:9090", help="手机内核控制器")
    parser.add_argument("--secret", default="", help="内核 secret（没设就留空）")
    parser.add_argument("--name", default=socket.gethostname(), help="设备名")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--flush", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.token and not args.dry_run:
        print("错误：需要 --token（PC 端 .env 里的 INGEST_TOKEN）", file=sys.stderr)
        return 2
    try:
        PhoneAgent(args).run()
    except KeyboardInterrupt:
        print("\n已停止", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
