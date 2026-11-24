"""Unit tests for the accounting core.  Run: python -m unittest discover -s tests -v"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collector import Collector  # noqa: E402
from app.main import cycle_window, local_midnight  # noqa: E402
from app.store import Store  # noqa: E402

TZ8 = timezone(timedelta(hours=8))


class FakeConfig:
    clash_api = "http://127.0.0.1:9"
    secret = ""
    secret_file = ""
    poll_interval = 1.0
    detail_days = 30


class FakeStore:
    def __init__(self):
        self.rows = []
        self.events = []

    def add_usage(self, rows):
        self.rows.extend(rows)

    def add_event(self, kind, detail=""):
        self.events.append((kind, detail))


def conn(cid, up, down, process="C:\\Apps\\OneDrive.exe", host="a.example.com",
         node="GBR", rule="DomainSuffix", payload="example.com"):
    return {
        "id": cid,
        "metadata": {"process": process, "host": host, "destinationPort": 443},
        "upload": up,
        "download": down,
        "rule": rule,
        "rulePayload": payload,
        "chains": [node, "Proxies", "Final"],
        "start": "2026-11-22T10:00:00+08:00",
    }


def payload(*connections, up_total=0, down_total=0):
    return {"uploadTotal": up_total, "downloadTotal": down_total, "connections": list(connections)}


class CollectorDiffTest(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.collector = Collector(FakeConfig(), self.store)
        # first poll only primes baselines and must not write anything
        self.collector._ingest(payload(), 1.0)

    def total(self):
        rows = self.store.rows
        return sum(r[5] for r in rows), sum(r[6] for r in rows)

    def test_first_poll_only_primes(self):
        store = FakeStore()
        collector = Collector(FakeConfig(), store)
        collector._ingest(
            payload(conn("a", 100, 1000), up_total=100, down_total=1000), 1.0
        )
        self.assertEqual(store.rows, [])

    def test_new_connection_counts_full_counters_once(self):
        self.collector._ingest(
            payload(conn("a", 100, 1000), up_total=100, down_total=1000), 1.0
        )
        self.assertEqual(self.total(), (100, 1000))

    def test_cumulative_counters_are_diffed(self):
        self.collector._ingest(
            payload(conn("a", 100, 1000), up_total=100, down_total=1000), 1.0
        )
        self.collector._ingest(
            payload(conn("a", 150, 1200), up_total=150, down_total=1200), 1.0
        )
        self.assertEqual(self.total(), (150, 1200))

    def test_vanished_connection_residual_is_attributed(self):
        self.collector._ingest(
            payload(conn("a", 100, 1000), up_total=100, down_total=1000), 1.0
        )
        # connection closed between polls: global counter still sees the tail
        self.collector._ingest(payload(up_total=100, down_total=1500), 1.0)
        self.assertEqual(self.total(), (100, 1500))
        self.assertEqual(self.store.rows[-1][1], "OneDrive.exe")

    def test_residual_without_vanished_goes_to_unattributed(self):
        self.collector._ingest(payload(up_total=0, down_total=300), 1.0)
        self.assertEqual(self.total(), (0, 300))
        self.assertEqual(self.store.rows[0][1], "(unattributed)")

    def test_counter_reset_does_not_double_count(self):
        self.collector._ingest(
            payload(conn("a", 5000, 5000), up_total=5000, down_total=5000), 1.0
        )
        self.collector._ingest(payload(conn("a", 10, 20), up_total=10, down_total=20), 1.0)
        self.assertEqual(self.total(), (5000, 5000))
        self.assertTrue(any(kind == "core_restart" for kind, _ in self.store.events))

    def test_process_basename_and_unknown_host(self):
        self.collector._ingest(
            payload(conn("a", 1, 1, process="", host=""), up_total=1, down_total=1), 1.0
        )
        row = self.store.rows[0]
        self.assertEqual(row[1], "(unknown)")
        self.assertEqual(row[3], "(unknown)")

    def test_direct_chain_kept_as_direct(self):
        item = conn("a", 5, 5)
        item["chains"] = ["DIRECT"]
        self.collector._ingest(payload(item, up_total=5, down_total=5), 1.0)
        self.assertEqual(self.store.rows[0][2], "DIRECT")


class SecretDiscoveryTest(unittest.TestCase):
    class DirConfig(FakeConfig):
        def __init__(self, path):
            self.secret_file = path
            self.secret = ""

    def test_reads_secret_from_config_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "config.yaml"), "w", encoding="utf-8") as handle:
                handle.write('external-controller: "127.0.0.1:9097"\nsecret: "abcDEF123456"\n')
            collector = Collector(self.DirConfig(tmp), FakeStore())
            self.assertEqual(collector._secret(), "abcDEF123456")
            self.assertEqual(collector.secret_source, "file")

    def test_prefers_shared_preferences_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "config.yaml"), "w", encoding="utf-8") as handle:
                handle.write('secret: "fromYaml12345"\n')
            with open(os.path.join(tmp, "shared_preferences.json"), "w", encoding="utf-8") as handle:
                handle.write('{"k":"\\"secret\\":\\"fromPrefs6789\\""}')
            collector = Collector(self.DirConfig(tmp), FakeStore())
            self.assertEqual(collector._secret(), "fromPrefs6789")

    def test_empty_directory_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = Collector(self.DirConfig(tmp), FakeStore())
            self.assertEqual(collector._secret(), "")


class StoreMathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.now = int(datetime(2026, 11, 22, 12, 0, tzinfo=TZ8).timestamp())
        self.minute = self.now - self.now % 60

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, node, up, down, process="chrome.exe", host="x.com", rule="Rule"):
        self.store.add_usage([(self.minute, process, node, host, rule, up, down)])

    def test_ratio_applies_and_direct_is_excluded(self):
        self.store.set_ratios([{"pattern": "^GBR", "ratio": 2.0}])
        self.add("GBR 英国 02", 100, 300)
        self.add("DIRECT", 50, 50)
        totals = self.store.totals(self.now - 3600, self.now + 3600, "updown")
        self.assertEqual(totals["billed"], 800)   # (100+300) * 2
        self.assertEqual(totals["direct"], 100)
        self.assertEqual(totals["raw"], 400)

    def test_billing_mode_down_only(self):
        self.store.set_ratios([{"pattern": ".*", "ratio": 1.5}])
        self.add("HKG 香港 01", 1000, 200)
        totals = self.store.totals(self.now - 3600, self.now + 3600, "down")
        self.assertEqual(totals["billed"], 300)  # 200 * 1.5

    def test_ratio_precedence_first_match_wins(self):
        self.store.set_ratios([
            {"pattern": "^GBR", "ratio": 3.0},
            {"pattern": ".*", "ratio": 1.0},
        ])
        self.assertEqual(self.store.ratio_for("GBR 英国 01"), 3.0)
        self.assertEqual(self.store.ratio_for("HKG 香港 01"), 1.0)

    def test_top_orders_by_billed_and_has_share(self):
        self.add("GBR", 100, 100, process="chrome.exe")
        self.add("HKG", 1000, 1000, process="OneDrive.exe")
        items = self.store.top(self.now - 3600, self.now + 3600, "process", 10, "updown")
        self.assertEqual(items[0]["name"], "OneDrive.exe")
        self.assertEqual(items[0]["billed"], 2000)
        self.assertGreater(items[0]["share"], items[1]["share"])

    def test_series_buckets_are_summed(self):
        self.add("GBR", 10, 10)
        points = self.store.series(self.now - 3600, self.now + 3600, 60, "updown")
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["billed"], 20)

    def test_settings_roundtrip(self):
        self.store.put_settings({"budget_gb": 200, "billing_mode": "down"})
        self.assertEqual(self.store.get_settings()["budget_gb"], 200)


class CycleWindowTest(unittest.TestCase):
    def ts(self, *args):
        return int(datetime(*args, tzinfo=TZ8).timestamp())

    def test_calendar_first_day(self):
        start, end = cycle_window(self.ts(2026, 3, 15, 12), 8, "calendar", 1, 30)
        self.assertEqual(start, self.ts(2026, 3, 1))
        self.assertEqual(end, self.ts(2026, 4, 1))

    def test_calendar_custom_day_before_today(self):
        start, end = cycle_window(self.ts(2026, 3, 15, 12), 8, "calendar", 20, 30)
        self.assertEqual(start, self.ts(2026, 2, 20))
        self.assertEqual(end, self.ts(2026, 3, 20))

    def test_calendar_custom_day_rolls_back_a_month(self):
        start, end = cycle_window(self.ts(2026, 3, 5, 12), 8, "calendar", 20, 30)
        self.assertEqual(start, self.ts(2026, 2, 20))
        self.assertEqual(end, self.ts(2026, 3, 20))

    def test_rolling(self):
        now = self.ts(2026, 11, 22, 12)
        start, end = cycle_window(now, 8, "rolling", 1, 30)
        self.assertEqual(end, now)
        self.assertEqual(start, now - 30 * 86400)

    def test_local_midnight(self):
        start, end = local_midnight(self.ts(2026, 11, 22, 23, 30), 8)
        self.assertEqual(start, self.ts(2026, 11, 22))
        self.assertEqual(end, self.ts(2026, 11, 23))


if __name__ == "__main__":
    unittest.main()
