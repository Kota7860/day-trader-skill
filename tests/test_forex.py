"""trader-forex: the pairs, the sessions, and the breakout scan on five real days of Yahoo bars
(November 20 to 26 2026) for all 21 pairs plus gold and silver futures."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

fx = load("trader-forex", "forex")
DIR = os.path.join(FIX, "trader-forex")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fx.main(argv)
    return code, out.getvalue(), err.getvalue()


class Universe(unittest.TestCase):
    def test_23_pairs_and_metals_every_one_a_market(self):
        self.assertEqual(len(fx.GROUPS["all"]), 23)
        self.assertEqual(len(set(fx.GROUPS["all"])), 23)
        for s in fx.GROUPS["all"]:
            self.assertNotEqual(fx.entry.instrument(s)["market"], "stock", s)
        for s in fx.GROUPS["metals"]:
            self.assertEqual(fx.entry.instrument(s)["market"], "metal", s)

    def test_every_pair_has_saved_bars(self):
        for s in fx.GROUPS["all"]:
            self.assertTrue(os.path.exists(os.path.join(DIR, fx.entry.instrument(s)["yahoo"] + ".json.gz")), s)

    def test_pairs_command(self):
        code, out, _ = run(["pairs"])
        self.assertEqual(code, 0)
        self.assertIn("GBPJPY   crosses  GBP/JPY   GBPJPY=X 3", out)
        self.assertIn("23 pairs and metals", out)


class Sessions(unittest.TestCase):
    def at(self, text):
        rows, market = fx.sessions_at(fx.entry.parse_now(text))
        return {r["key"]: r for r in rows}, market

    def test_london_new_york_overlap(self):
        s, market = self.at("2026-11-23 09:00")
        self.assertTrue(market)
        self.assertEqual([k for k in s if s[k]["open"]], ["london", "ny"])
        self.assertEqual((s["london"]["opens_ny"], s["london"]["closes_ny"]), ("Mon 03:00", "Mon 11:30"))

    def test_tokyo_and_london_overlap_early_morning(self):
        s, _ = self.at("2026-11-23 03:30")
        self.assertEqual([k for k in s if s[k]["open"]], ["tokyo", "london"])
        self.assertEqual(s["tokyo"]["opens_ny"], "Sun 19:00")

    def test_sydney_moves_with_its_own_summer_time(self):
        s, _ = self.at("2026-11-23 16:00")              # Sydney is on daylight time (+11) in November
        self.assertEqual(s["sydney"]["opens_ny"], "Mon 15:00")
        self.assertTrue(s["sydney"]["open"])
        s, _ = self.at("2026-07-14 18:00")              # July: Sydney +10, New York -4
        self.assertEqual(s["sydney"]["opens_ny"], "Tue 17:00")

    def test_the_weekend_is_closed(self):
        _, market = self.at("2026-11-21 12:00")         # Saturday
        self.assertFalse(market)
        _, market = self.at("2026-11-22 16:59")         # Sunday, a minute before the week opens
        self.assertFalse(market)
        _, market = self.at("2026-11-22 17:00")
        self.assertTrue(market)
        code, out, _ = run(["sessions", "--now", "2026-11-21 12:00"])
        self.assertEqual(code, 1)
        self.assertIn("Forex market CLOSED", out)


class Scan(unittest.TestCase):
    def scan(self, now, **kw):
        return fx.scan(kw.pop("symbols", fx.GROUPS["all"]), fx.entry.parse_now(now), offline=DIR, **kw)

    def test_new_york_monday_mid_morning(self):
        res = self.scan("2026-11-23 10:00")
        self.assertEqual((len(res["hits"]), len(res["failed"])), (23, 0))
        first = res["hits"][0]
        self.assertEqual((first["symbol"], first["side"], first["time"], first["price"], first["level"]),
                         ("AUDJPY", "sell", "8:35 AM", 105.157, 105.177))
        by = {h["symbol"]: h for h in res["hits"]}
        self.assertEqual((by["EURAUD"]["side"], by["EURAUD"]["price"], by["EURAUD"]["beyond"]),
                         ("buy", 1.65502, "+4.1 pips"))
        self.assertEqual((by["XAUUSD"]["side"], by["XAUUSD"]["beyond"]), ("buy", "+0.63%"))

    def test_hits_match_trader_entry(self):
        res = self.scan("2026-11-23 10:00", symbols=["EURAUD"])
        chart = fx.entry.get_chart("EURAUD", "5m", offline=os.path.join(DIR, "EURAUD=X.json.gz"))
        r = fx.entry.evaluate(chart, fx.entry.parse_now("2026-11-23 10:00"), or_minutes=30, symbol="EURAUD")
        self.assertEqual((r["status"], r["trigger"]["price"]), ("triggered", res["hits"][0]["price"]))

    def test_nothing_before_the_range_is_set(self):
        res = self.scan("2026-11-23 08:20")
        self.assertEqual(res["hits"], [])

    def test_near_breakouts_exclude_pairs_that_broke_out(self):
        res = self.scan("2026-11-23 08:45")
        hit = {h["symbol"] for h in res["hits"]}
        self.assertTrue(res["waiting"])
        self.assertFalse(hit & {w["symbol"] for w in res["waiting"]})
        self.assertEqual(res["waiting"][0]["symbol"], "EURUSD")

    def test_london_session(self):
        res = self.scan("2026-11-24 05:00", symbols=fx.GROUPS["majors"], session="london")
        by = {h["symbol"]: (h["side"], h["time"]) for h in res["hits"]}
        self.assertEqual(by["GBPUSD"], ("buy", "3:35 AM"))
        self.assertEqual(by["USDJPY"], ("sell", "3:35 AM"))

    def test_the_weekend_has_no_hits(self):
        res = self.scan("2026-11-21 10:00", symbols=fx.GROUPS["majors"])
        self.assertEqual(res["hits"], [])
        self.assertIn("closed", res["closed"])


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.out = os.path.join(self.tmp, "watchlist.json")

    def test_scan_writes_a_watchlist_trader_entry_reads(self):
        code, text, _ = run(["scan", "--group", "majors", "--now", "2026-11-23 10:00", "--offline", DIR,
                             "--out", self.out, "--config", os.path.join(self.tmp, "none.json")])
        self.assertEqual(code, 0)
        self.assertIn("New York-session breakout scan", text)
        self.assertIn("needs \"allow_short\": true", text)
        with open(self.out) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["created_by"], doc["session"], doc["universe"], doc["failed"]),
                         ("trader-forex", "ny", 7, 0))
        self.assertEqual(sorted(h["symbol"] for h in doc["hits"]), sorted(fx.GROUPS["majors"]))
        self.assertEqual(set(doc["hits"][0]), {"symbol", "market", "side", "session", "time", "price", "level"})

    def test_no_hits_writes_nothing(self):
        code, text, _ = run(["scan", "--now", "2026-11-23 08:20", "--offline", DIR, "--out", self.out])
        self.assertEqual(code, 1)
        self.assertIn("No pair has closed beyond its range yet", text)
        self.assertFalse(os.path.exists(self.out))

    def test_stocks_are_refused(self):
        code, _, err = run(["scan", "--symbols", "EURUSD,TSLA", "--offline", DIR, "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("/trader-scanner", err)

    def test_a_missing_pair_is_reported_not_fatal(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        code, text, _ = run(["scan", "--now", "2026-11-23 10:00", "--offline", empty, "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("Could not read 23 of 23", text)


if __name__ == "__main__":
    unittest.main()
