import contextlib
import io
import gzip
import http.client
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import fuekw_watch as fw
from fuekw_watch import Volume, decide, parse_pass

FIXTURE = Path(__file__).parent / "fixtures" / "pass_2026-10-28.html"
TUE = datetime(2026, 10, 27, 12, 0)
MON = datetime(2026, 10, 26, 10, 0)  # a Monday


def snippet(value, total="30", unit="GB", period="Oktober 2026", total_unit=None):
    return (
        f'<div class="remaining-volume-value">{value}\n</div><div class="volume-unit">{unit}</div>'
        f'<div class="start-volume">{total}</div><div class="volume-unit">{total_unit or unit}</div>'
        f'<div class="info-row">Abrechnungsmonat: {period}\n</div>'
    )


class ParseTests(unittest.TestCase):
    def test_parse_fixture(self):
        self.assertEqual(parse_pass(FIXTURE.read_text()), Volume(1.48, 30.0, "Oktober 2026"))

    def test_parse_comma_decimal(self):
        self.assertEqual(parse_pass(snippet("1.234,5")).remaining_gb, 1234.5)

    def test_parse_mb_unit(self):
        v = parse_pass(snippet("500", total="1000", unit="MB"))
        self.assertEqual((v.remaining_gb, v.total_gb), (0.5, 1.0))

    def test_parse_mixed_units_below_one_gb(self):
        # live page on 29.10.2026: "905,31 MB / 30 GB"
        v = parse_pass(snippet("905,31", unit="MB", total_unit="GB"))
        self.assertEqual((v.remaining_gb, v.total_gb), (0.90531, 30.0))

    def test_parse_unknown_layout_returns_none(self):
        self.assertIsNone(parse_pass("<html>Page not found</html>"))


class DecideTests(unittest.TestCase):
    def test_low_alert_once(self):
        msgs, st = decide(Volume(5, 30, "Oktober 2026"), True, {}, TUE)
        self.assertEqual(len(msgs), 1)
        self.assertIn("unter 20 %", msgs[0])
        self.assertEqual(decide(Volume(5, 30, "Oktober 2026"), True, st, TUE)[0], [])

    def test_jump_to_throttled_sends_both(self):
        msgs, st = decide(Volume(0, 30, "Oktober 2026"), True, {}, TUE)
        text = "\n".join(msgs)
        self.assertIn("unter 20 %", text)
        self.assertIn("gedrosselt", text)
        self.assertEqual(st["alerted"], ["low", "throttled"])

    def test_new_period_resets_alerts(self):
        state = {"period": "Oktober 2026", "alerted": ["low"]}
        msgs, st = decide(Volume(25, 30, "November 2026"), True, state, TUE)
        self.assertEqual((msgs, st["alerted"]), ([], []))

    def test_broken_reported_once(self):
        m0, st = decide(None, True, {}, TUE)
        m1, st = decide(None, True, st, TUE)
        m2, st = decide(None, True, st, TUE)
        self.assertEqual(m0, [])
        self.assertEqual(len(m1), 1)
        self.assertIn("Volumen-Abfrage kaputt", m1[0])
        self.assertEqual(m2, [])
        m3, st = decide(Volume(25, 30, "Oktober 2026"), True, st, TUE)
        self.assertEqual(len(m3), 1)
        self.assertIn("wieder", m3[0])
        self.assertFalse(st["pass_broken"])

    def test_hub_down_and_recovery(self):
        v = Volume(25, 30, "Oktober 2026")
        m0, st = decide(v, False, {}, TUE)
        m1, st = decide(v, False, st, TUE)
        m2, st = decide(v, False, st, TUE)
        self.assertEqual(m0, [])
        self.assertEqual(len(m1), 1)
        self.assertIn("Hub nicht erreichbar", m1[0])
        self.assertEqual(m2, [])
        m3, _ = decide(v, True, st, TUE)
        self.assertEqual(len(m3), 1)
        self.assertIn("wieder erreichbar", m3[0])

    def test_single_blip_stays_silent(self):
        # mobile uplink drops briefly: one failed run must not alert or need an all-clear
        v = Volume(25, 30, "Oktober 2026")
        m1, st = decide(None, False, {}, TUE)
        m2, st = decide(v, True, st, TUE)
        m3, st = decide(None, False, st, TUE)
        self.assertEqual((m1, m2, m3), ([], [], []))

    def test_weekly_summary_monday(self):
        v = Volume(1.48, 30, "Oktober 2026")
        line = "FüKw-Volumen: 1,48 von 30 GB übrig (Oktober 2026)"
        st = {"period": "Oktober 2026", "alerted": ["low"], "last_weekly": "2026-10-19"}
        msgs, st = decide(v, True, st, MON)
        self.assertTrue(any(line in m for m in msgs))
        self.assertEqual(decide(v, True, st, MON)[0], [])

    def test_weekly_not_before_nine_or_on_other_days(self):
        v = Volume(25, 30, "Oktober 2026")
        self.assertEqual(decide(v, True, {}, datetime(2026, 10, 26, 8, 0))[0], [])
        self.assertEqual(decide(v, True, {}, TUE)[0], [])


class RunOnceTests(unittest.TestCase):
    def test_failed_send_keeps_alert_pending(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            html = FIXTURE.read_text()

            def boom(text):
                raise OSError("telegram down")

            with self.assertRaises(OSError):
                fw.run_once(lambda: html, lambda: True, boom, path, TUE)
            self.assertFalse(path.exists())

            sent = []
            fw.run_once(lambda: html, lambda: True, sent.append, path, TUE)
            self.assertEqual(len(sent), 1)
            self.assertEqual(json.loads(path.read_text())["alerted"], ["low"])
            fw.run_once(lambda: html, lambda: True, sent.append, path, TUE)
            self.assertEqual(len(sent), 1)

    def test_reading_is_recorded_and_unreachable_history_is_harmless(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            html = FIXTURE.read_text()
            got = []
            fw.run_once(lambda: html, lambda: True, lambda t: None, path, TUE, got.append)
            self.assertEqual(got[0].period, "Oktober 2026")
            fw.run_once(lambda: None, lambda: True, lambda t: None, path, TUE, got.append)
            self.assertEqual(len(got), 1)  # nothing to record without a reading
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                fw._record("http://127.0.0.1:9/api/reading", "t", got[0])  # closed port: logs, no raise
            self.assertIn("volumen:", err.getvalue())

    def test_corrupt_state_is_ignored_and_rewritten(self):
        html = FIXTURE.read_text()
        for junk in ("{not json", "[1, 2]", ""):
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "state.json"
                path.write_text(junk)
                fw.run_once(lambda: html, lambda: True, lambda t: None, path, TUE)
                self.assertEqual(json.loads(path.read_text())["alerted"], ["low"])

    def test_malformed_last_weekly_counts_as_missing(self):
        v = Volume(25, 30, "Oktober 2026")
        for bad in ("garbage", 5, None):
            msgs, _ = decide(v, True, {"last_weekly": bad}, MON)
            self.assertEqual(len(msgs), 1)


class FetcherTests(unittest.TestCase):
    def test_fetchers_swallow_protocol_errors(self):
        for exc in (http.client.IncompleteRead(b"x"), ValueError("bad url")):
            with mock.patch("urllib.request.urlopen", side_effect=exc):
                self.assertIsNone(fw._fetch_pass())
                self.assertFalse(fw._check_hub("http://x"))

    def test_truncated_gzip_returns_none(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.headers = {"Content-Encoding": "gzip"}
        resp.read.return_value = gzip.compress(b"<html>" * 100)[:-10]
        with mock.patch("urllib.request.urlopen", return_value=resp):
            self.assertIsNone(fw._fetch_pass())


class StateTypeTests(unittest.TestCase):
    def test_wrongly_typed_state_is_normalized(self):
        html = FIXTURE.read_text()
        bad = {"period": "Oktober 2026", "alerted": 5, "last_weekly": 7}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text(json.dumps(bad))
            fw.run_once(lambda: html, lambda: True, lambda t: None, path, TUE)
            self.assertEqual(json.loads(path.read_text())["alerted"], ["low"])


if __name__ == "__main__":
    unittest.main()
