"""Forex and metals: the entry timer and the exit planner on real Yahoo bars.

Fixtures (tests/fixtures/trader-markets) are Yahoo's 5-minute charts for the five
days to Wednesday November 11 2026: EURUSD=X, USDJPY=X, EURGBP=X and GC=F (COMEX
gold futures, the free stand-in for spot gold). Stocks must behave exactly as before.
"""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

entry = load("trader-entry", "entry")
exitp = load("trader-exit", "exit")
MK = os.path.join(FIX, "trader-markets")
EUR = os.path.join(MK, "EURUSD=X-5d5m.json.gz")
JPY = os.path.join(MK, "USDJPY=X-5d5m.json.gz")
GOLD = os.path.join(MK, "GC=F-5d5m.json.gz")


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class Instruments(unittest.TestCase):
    def test_a_pair_is_forex_however_it_is_written(self):
        for text in ("EURUSD", "eur/usd", "EUR_USD", "EURUSD=X", "EUR-USD"):
            i = entry.instrument(text)
            self.assertEqual((i["market"], i["symbol"], i["yahoo"]), ("forex", "EURUSD", "EURUSD=X"), text)
            self.assertEqual((i["decimals"], round(i["pip"], 6)), (5, 0.0001))

    def test_yen_pairs_quote_to_three_decimals(self):
        i = entry.instrument("USDJPY")
        self.assertEqual((i["decimals"], round(i["pip"], 6), i["quote"]), (3, 0.01, "JPY"))

    def test_gold_and_silver_read_comex_futures(self):
        self.assertEqual(entry.instrument("XAUUSD")["yahoo"], "GC=F")
        self.assertEqual(entry.instrument("xau/usd")["symbol"], "XAUUSD")
        self.assertEqual(entry.instrument("GC=F")["symbol"], "XAUUSD")
        s = entry.instrument("XAGUSD")
        self.assertEqual((s["market"], s["yahoo"], s["decimals"]), ("metal", "SI=F", 3))

    def test_stock_tickers_stay_stocks(self):
        for text in ("TSLA", "BRK.B", "GOOGL", "F", "GOLD", "EURO", "USDX"):
            self.assertEqual(entry.instrument(text)["market"], "stock", text)
        self.assertEqual(entry.instrument("BRK.B")["yahoo"], "BRK-B")

    def test_both_tools_agree(self):
        for text in ("EURUSD", "USDJPY", "XAUUSD", "XAGUSD", "TSLA"):
            self.assertEqual(entry.instrument(text), exitp.instrument(text), text)


class Sessions(unittest.TestCase):
    def test_new_york_session_is_eight_to_five(self):
        inst = entry.instrument("EURUSD")
        o, c = entry.market_session(inst, datetime.date(2026, 11, 9), "ny")
        self.assertEqual((o.strftime("%H:%M"), c.strftime("%H:%M")), ("08:00", "17:00"))

    def test_london_open_in_new_york_time_follows_both_clocks(self):
        inst = entry.instrument("EURUSD")
        for day, want in (((2026, 11, 9), "03:00"),    # both on winter time
                          ((2026, 7, 14), "03:00"),    # both on summer time
                          ((2026, 3, 16), "04:00"),    # New York already on summer time, London not yet
                          ((2026, 10, 27), "04:00")):  # London back on winter time, New York not yet
            o, _ = entry.market_session(inst, datetime.date(*day), "london")
            self.assertEqual(o.strftime("%H:%M"), want, day)

    def test_weekends_and_the_two_fixed_holidays_are_closed(self):
        inst = entry.instrument("EURUSD")
        self.assertIsNone(entry.market_session(inst, datetime.date(2026, 11, 7), "ny"))     # Saturday
        self.assertIsNone(entry.market_session(inst, datetime.date(2026, 12, 25), "ny"))
        self.assertIsNone(entry.market_session(inst, datetime.date(2027, 1, 1), "ny"))
        self.assertIsNotNone(entry.market_session(inst, datetime.date(2026, 11, 26), "ny"))  # Thanksgiving: forex is open

    def test_metals_are_closed_on_good_friday_but_forex_is_not(self):
        d = datetime.date(2026, 4, 3)
        self.assertIsNone(entry.market_session(entry.instrument("XAUUSD"), d, "ny"))
        self.assertIsNotNone(entry.market_session(entry.instrument("EURUSD"), d, "ny"))


class EntryForex(unittest.TestCase):
    def check(self, sym, path, now, **kw):
        chart = entry.get_chart(sym, "5m", offline=path)
        opts = dict(or_minutes=30, symbol=entry.instrument(sym)["symbol"])
        opts.update(kw)
        return entry.evaluate(chart, entry.parse_now(now), **opts)

    def test_eurusd_new_york_breakout(self):
        r = self.check("EURUSD", EUR, "2026-11-09 08:55")
        self.assertEqual((r["status"], r["symbol"], r["market"], r["session"]), ("triggered", "EURUSD", "forex", "ny"))
        self.assertEqual(r["opening_range"]["high"], 1.11995)
        self.assertEqual(r["opening_range"]["low"], 1.11957)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"], r["trigger"]["bar_index"]), ("8:55 AM", 1.1202, 10))
        self.assertEqual((r["opens"], r["closes"], r["no_entries_before"], r["no_entries_after"]),
                         ("08:00", "17:00", "08:30", "15:30"))

    def test_the_bar_before_the_trigger_is_still_waiting(self):
        r = self.check("EURUSD", EUR, "2026-11-09 08:50")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertIn("under the range high 1.11995", r["reason"])
        self.assertIn("pips", r["reason"])

    def test_range_still_forming_and_pre_open(self):
        r = self.check("EURUSD", EUR, "2026-11-09 08:10")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        r = self.check("EURUSD", EUR, "2026-11-09 07:30")
        self.assertEqual(r["phase"], "pre-open")
        self.assertIn("New York session opens at 8:00", r["reason"])

    def test_london_session_uses_the_london_open(self):
        r = self.check("EURUSD", EUR, "2026-11-10 11:00", session_name="london")
        self.assertEqual((r["opens"], r["closes"]), ("03:00", "11:30"))
        self.assertEqual((r["status"], r["trigger"]["time"], r["trigger"]["price"]), ("triggered", "3:35 AM", 1.12499))

    def test_a_sell_triggers_on_a_close_below_the_range_low(self):
        r = self.check("EURUSD", EUR, "2026-11-10 11:00", side="sell")
        self.assertEqual((r["status"], r["side"], r["level"]), ("triggered", "sell", 1.1212))
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"]), ("8:35 AM", 1.11857))
        self.assertIn("closed below the range low", r["reason"])

    def test_no_breakout_expires_at_the_cutoff(self):
        r = self.check("EURUSD", EUR, "2026-11-10 16:00")
        self.assertEqual(r["status"], "expired")
        self.assertIn("by 15:30", r["reason"])

    def test_yen_prices_keep_three_decimals(self):
        r = self.check("USDJPY", JPY, "2026-11-10 11:00")
        self.assertEqual((r["status"], r["trigger"]["price"], r["level"]), ("triggered", 158.38, 158.235))
        self.assertIn("+14.5 pips", "\n".join(entry.describe(r)))

    def test_gold_reads_the_futures_chart_and_says_so(self):
        r = self.check("XAUUSD", GOLD, "2026-11-10 11:00", side="sell")
        self.assertEqual((r["symbol"], r["market"], r["status"]), ("XAUUSD", "metal", "triggered"))
        self.assertEqual((r["trigger"]["price"], r["level"]), (4052.2, 4075.6))
        text = "\n".join(entry.describe(r))
        self.assertIn("COMEX gold futures", text)
        self.assertIn("$4052.20", text)

    def test_the_weekend_is_closed(self):
        r = self.check("EURUSD", EUR, "2026-11-07 10:00")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertIn("forex market is closed", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-09 08:00")

    def test_the_wrong_chart_file_is_refused(self):
        with self.assertRaises(entry.EntryError):
            entry.get_chart("GBPUSD", "5m", offline=EUR)


class EntryCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "config.json")
        self.addCleanup(shutil.rmtree, self.tmp)

    def cfg_with(self, **kw):
        with open(self.cfg, "w") as fh:
            json.dump(kw, fh)

    def test_check_writes_an_entry_with_the_market(self):
        out = os.path.join(self.tmp, "entry.json")
        code, text, _ = run(entry, ["check", "eur/usd", "--now", "2026-11-09 08:55", "--offline", EUR,
                                    "--config", self.cfg, "--write", out])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 8:55 AM at 1.12020", text)
        self.assertIn("(+2.5 pips)", text)
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["symbol"], doc["side"], doc["market"], doc["session"], doc["price"]),
                         ("EURUSD", "buy", "forex", "ny", 1.1202))
        self.assertEqual(doc["opening_range"], {"high": 1.11995, "low": 1.11957, "minutes": 30})

    def test_a_sell_is_refused_until_shorts_are_turned_on(self):
        args = ["check", "EURUSD", "--side", "sell", "--now", "2026-11-10 11:00", "--offline", EUR, "--config", self.cfg]
        code, _, err = run(entry, args)
        self.assertEqual(code, 1)
        self.assertIn('"allow_short": true', err)
        self.cfg_with(allow_short=True)
        code, text, _ = run(entry, args)
        self.assertEqual(code, 0)
        self.assertIn("closed below the range low 1.12120 (-26.3 pips)", text)

    def test_a_stock_short_is_still_refused_even_with_shorts_on(self):
        self.cfg_with(allow_short=True)
        tsla = os.path.join(FIX, "trader-entry", "TSLA-2026-10-19-5m.json")
        code, _, err = run(entry, ["check", "TSLA", "--side", "sell", "--offline", tsla, "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("longs only", err)

    def test_the_session_can_come_from_the_config(self):
        self.cfg_with(markets={"forex": {"session": "london"}})
        code, text, _ = run(entry, ["check", "EURUSD", "--now", "2026-11-10 11:00", "--offline", EUR, "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("London-session range breakout", text)

    def test_volume_filters_are_refused_for_forex(self):
        code, _, err = run(entry, ["check", "EURUSD", "--min-volume-x", "1.5", "--offline", EUR, "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("no volume", err)

    def test_watch_replays_the_session_and_stops_on_the_trigger(self):
        out = os.path.join(self.tmp, "entry.json")
        code, text, _ = run(entry, ["watch", "EURUSD", "--now", "2026-11-09 07:50", "--offline", EUR,
                                    "--config", self.cfg, "--out", out])
        self.assertEqual(code, 0)
        self.assertIn("opening range set: high 1.11995, low 1.11957", text)
        self.assertIn("TRIGGERED: closed above 1.11995 (+2.5 pips)", text)
        self.assertTrue(os.path.exists(out))


class ExitForex(unittest.TestCase):
    def test_long_plan_to_the_pip(self):
        inst = exitp.instrument("EURUSD")
        c = exitp.compute(1.1202, "or_mid", None, "r", exitp.D("1.5"), 1.11995, 1.11957, inst=inst)
        self.assertEqual((str(c["stop"]), str(c["target"]), str(c["risk"])), ("1.11976", "1.12086", "0.00044"))

    def test_pips_stop(self):
        inst = exitp.instrument("EURUSD")
        c = exitp.compute(1.1202, "pips", exitp.D("10"), "r", exitp.D("2"), inst=inst)
        self.assertEqual((str(c["stop"]), str(c["target"])), ("1.11920", "1.12220"))
        jpy = exitp.instrument("USDJPY")
        c = exitp.compute(158.38, "pips", exitp.D("15"), "r", exitp.D("2"), inst=jpy)
        self.assertEqual((str(c["stop"]), str(c["target"])), ("158.230", "158.680"))

    def test_pips_stop_is_forex_only(self):
        with self.assertRaises(exitp.PlanError):
            exitp.compute(4052.2, "pips", exitp.D("10"), "r", exitp.D("2"), inst=exitp.instrument("XAUUSD"))

    def test_short_plan_mirrors_the_long(self):
        inst = exitp.instrument("EURUSD")
        c = exitp.compute(1.11857, "or_mid", None, "r", exitp.D("1"), 1.12183, 1.1212, inst=inst, side="sell")
        self.assertEqual(str(c["stop"]), "1.12152")          # 1.121515 rounds UP, away from a short's entry
        self.assertEqual(str(c["target"]), "1.11562")
        self.assertEqual(str(c["reward_risk"]), "1.00")

    def test_a_short_stop_under_the_entry_is_refused(self):
        inst = exitp.instrument("EURUSD")
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.compute(1.1186, "price", exitp.D("1.1150"), "r", exitp.D("1"), inst=inst, side="sell")
        self.assertIn("a short's stop goes above", str(cm.exception))

    def test_a_stop_inside_the_spread_is_refused(self):
        inst = exitp.instrument("EURUSD")
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.compute(1.1202, "pips", exitp.D("1"), "r", exitp.D("2"), inst=inst)
        self.assertIn("too tight", str(cm.exception))

    def test_gold_rounds_to_the_cent(self):
        inst = exitp.instrument("XAUUSD")
        c = exitp.compute(4052.2, "or_low", None, "r", exitp.D("1"), 4088.0, 4075.6, inst=inst, side="sell")
        self.assertEqual((str(c["stop"]), str(c["target"]), str(c["risk"])), ("4075.60", "4028.80", "23.40"))


class ExitCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cfg = os.path.join(self.tmp, "config.json")
        self.entry = os.path.join(self.tmp, "entry.json")
        self.plan = os.path.join(self.tmp, "plan.json")
        run(entry, ["check", "EURUSD", "--now", "2026-11-09 08:55", "--offline", EUR, "--config", self.cfg,
                    "--write", self.entry])

    def test_plan_from_the_entry_file(self):
        code, text, _ = run(exitp, ["plan", "--from", self.entry, "--target", "1.5R", "--now", "2026-11-09 08:55",
                                    "--config", self.cfg, "--write", self.plan])
        self.assertEqual(code, 0)
        self.assertIn("EURUSD long", text)
        self.assertIn("Risk a unit   0.00044 USD = 4.4 pips", text)
        with open(self.plan) as fh:
            p = json.load(fh)
        self.assertEqual((p["entry"], p["stop"], p["target"], p["risk_per_share"]), (1.1202, 1.11976, 1.12086, 0.00044))
        self.assertEqual((p["market"], p["session"], p["decimals"], p["risk_pips"], p["flat_by"], p["side"]),
                         ("forex", "ny", 5, 4.4, "16:55", "buy"))

    def test_atr_stop_reads_the_session_bars(self):
        code, text, _ = run(exitp, ["plan", "--from", self.entry, "--stop", "atr:1.5", "--offline", EUR,
                                    "--now", "2026-11-09 08:55", "--config", self.cfg, "--json"])
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["atr_bars"], p["stop"]), (11, 1.11975))

    def test_london_plans_are_flat_before_the_london_close(self):
        code, text, _ = run(exitp, ["plan", "--symbol", "USD/JPY", "--entry", "158.38", "--stop", "pips:15",
                                    "--session", "london", "--date", "2026-11-10", "--time", "8:35 AM",
                                    "--now", "2026-11-10 08:35", "--config", self.cfg, "--json"])
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["symbol"], p["flat_by"], p["stop"], p["risk_pips"]), ("USDJPY", "11:25", 158.23, 15.0))

    def test_flat_by_cannot_cross_the_rollover(self):
        code, _, err = run(exitp, ["plan", "--from", self.entry, "--flat-by", "17:30", "--now", "2026-11-09 08:55",
                                   "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("overnight hold", err)

    def test_short_needs_allow_short(self):
        args = ["plan", "--symbol", "XAUUSD", "--entry", "4052.2", "--or-high", "4088", "--or-low", "4075.6",
                "--side", "sell", "--stop", "or-low", "--target", "1R", "--date", "2026-11-10", "--time", "8:35 AM",
                "--now", "2026-11-10 08:35", "--config", self.cfg]
        code, _, err = run(exitp, args)
        self.assertEqual(code, 1)
        self.assertIn('"allow_short": true', err)
        with open(self.cfg, "w") as fh:
            json.dump({"allow_short": True}, fh)
        code, text, _ = run(exitp, args)
        self.assertEqual(code, 0)
        self.assertIn("XAUUSD short", text)
        self.assertIn("Stop          $4,075.60", text)
        self.assertIn("COMEX gold futures", text)
        self.assertIn("loses when the price RISES", text)

    def test_a_stock_short_is_still_refused(self):
        with open(self.cfg, "w") as fh:
            json.dump({"allow_short": True}, fh)
        code, _, err = run(exitp, ["plan", "--symbol", "TSLA", "--entry", "364.18", "--side", "sell",
                                   "--stop", "pct:1", "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("longs only", err)


if __name__ == "__main__":
    unittest.main()
