import json
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import app

TZ = app.TZ


def at(day, hour=12, month=11):
    return datetime(2026, month, day, hour, tzinfo=TZ)


class SummaryTests(unittest.TestCase):
    def test_month_of(self):
        self.assertEqual(app.month_of("Oktober 2026"), (2026, 10))
        self.assertEqual(app.month_of("März 2027"), (2027, 3))
        self.assertIsNone(app.month_of("Quatsch"))

    def test_daily_usage_and_projection(self):
        # 1 GB per day from the 1st, 30 GB quota, 30-day month
        rows = [(at(d, 23).isoformat(), 30 - d, 30) for d in range(1, 11)]
        s = app.summarize(rows, "November 2026")
        self.assertEqual(s["days"], 30)
        self.assertAlmostEqual(s["daily"][1], 1)
        self.assertAlmostEqual(s["daily"][10], 1)
        self.assertAlmostEqual(s["avg"], 10 / 9.958, places=2)
        self.assertGreater(s["projected"], 29)
        self.assertIsNone(s["exhausted"])

    def test_history_starting_mid_month(self):
        # first reading on the 28th already shows 28.5 GB used: no 28.5 GB bar for that day
        rows = [(at(28, 16, 10).isoformat(), 1.5, 30), (at(28, 22, 10).isoformat(), 1.2, 30)]
        s = app.summarize(rows, "Oktober 2026")
        self.assertAlmostEqual(s["daily"][28], 0.3)
        self.assertAlmostEqual(s["avg"], 28.8 / (27 + 22 / 24), places=3)
        self.assertLess(s["exhaust_day"], 31)

    def test_exhausted_and_pass_top_up(self):
        rows = [(at(20).isoformat(), 0, 30), (at(21).isoformat(), 4, 35)]  # SpeedOn adds 5 GB
        s = app.summarize(rows, "November 2026")
        self.assertEqual(s["exhausted"], at(20))
        self.assertAlmostEqual(s["daily"][21], 1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "v.db")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(self.db, "s3cret", "https://status.example"))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def post(self, body, token="s3cret"):
        req = urllib.request.Request(self.base + "/api/reading", json.dumps(body).encode(),
                                     {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_ingest_requires_token_and_valid_body(self):
        good = {"period": "Oktober 2026", "remaining_gb": 1.48, "total_gb": 30}
        self.assertEqual(self.post(good, token="wrong"), 401)
        self.assertEqual(self.post({"period": "x"}), 400)
        self.assertEqual(self.post({**good, "remaining_gb": "nan"}), 400)
        self.assertEqual(self.post(good), 204)
        n = sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        self.assertEqual(n, 1)

    def test_page_renders_empty_and_with_data(self):
        page = urllib.request.urlopen(self.base + "/").read().decode()
        self.assertIn("Noch keine Messwerte", page)
        self.post({"period": "Oktober 2026", "remaining_gb": 1.48, "total_gb": 30})
        page = urllib.request.urlopen(self.base + "/").read().decode()
        self.assertIn("Oktober 2026", page)
        self.assertIn("28,52 / 30,00 GB", page)
        self.assertIn('href="https://status.example"', page)
        csv = urllib.request.urlopen(self.base + "/api/readings.csv").read().decode()
        self.assertIn(",Oktober 2026,1.48,30.0", csv)


if __name__ == "__main__":
    unittest.main()
