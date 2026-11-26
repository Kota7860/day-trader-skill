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
size = load("trader-budget", "size")
check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")
alerts = load("trader-alerts", "alerts")
tm = load("trader-timemachine", "timemachine")
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

    def test_every_tool_agrees(self):
        for text in ("EURUSD", "USD/JPY", "XAUUSD", "XAGUSD", "TSLA", "BRK.B"):
            want = entry.instrument(text)
            for mod in (exitp, size, check, ticket):
                self.assertEqual(mod.instrument(text), want, "%s in %s" % (text, mod.__name__))


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


EUR_PLAN = {"symbol": "EURUSD", "side": "buy", "date": "2026-11-09", "setup": "orb", "entry": 1.1202,
            "entry_time": "8:55 AM", "stop": 1.11976, "target": 1.12086, "flat_by": "16:55",
            "market": "forex", "session": "ny"}
GOLD_SHORT = {"symbol": "XAUUSD", "side": "sell", "date": "2026-11-10", "setup": "orb", "entry": 4052.2,
              "entry_time": "8:35 AM", "stop": 4075.6, "target": 4028.8, "flat_by": "16:55",
              "market": "metal", "session": "ny"}
CFG = {"budget": 5000}
GCFG = {"budget": 10000, "max_order_usd": 8000, "allow_short": True}   # one ounce of gold is about $4,050
LEV = {"budget": 5000, "allow_margin": True, "allow_short": True, "markets": {"forex": {"max_leverage": 20}}}


class Sizing(unittest.TestCase):
    def test_without_leverage_the_position_cap_binds(self):
        s = size.size_market_trade(EUR_PLAN, CFG, today="2026-11-09")
        self.assertEqual((s["qty"], s["bound_by"], s["leverage"]), (3347, "position cap", 1.0))
        self.assertEqual((s["cost"], s["max_loss"], s["risk_pips"]), (3749.31, 1.47, 4.4))
        self.assertIsNone(s["refusal"])

    def test_leverage_raises_the_size_but_never_the_max_loss(self):
        s = size.size_market_trade(EUR_PLAN, LEV, today="2026-11-09")
        self.assertEqual((s["qty"], s["leverage"]), (66952, 20.0))
        self.assertEqual((s["cost"], s["margin"], s["max_loss"]), (74999.63, 3749.98, 29.46))
        wide = dict(EUR_PLAN, stop=1.1152, target=1.1302)             # 50 pips: now the max loss binds
        s = size.size_market_trade(wide, LEV, today="2026-11-09")
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss"]), (10000, "max loss", 50.0))

    def test_leverage_over_the_retail_cap_is_refused(self):
        with self.assertRaises(size.SizeError):
            size.size_market_trade(EUR_PLAN, dict(LEV, markets={"forex": {"max_leverage": 100}}), today="2026-11-09")

    def test_micro_lot_step(self):
        cfg = dict(LEV, markets={"forex": {"max_leverage": 20, "unit_step": 1000}})
        self.assertEqual(size.size_market_trade(EUR_PLAN, cfg, today="2026-11-09")["qty"], 66000)

    def test_a_dollar_base_pair_converts_the_loss_at_its_own_price(self):
        plan = {"symbol": "USDJPY", "side": "buy", "date": "2026-11-10", "entry": 158.38, "stop": 158.23,
                "target": 158.68}
        s = size.size_market_trade(plan, LEV, today="2026-11-10")
        self.assertAlmostEqual(s["quote_usd"], 1 / 158.23, places=8)
        self.assertAlmostEqual(s["unit_usd"], 158.38 / 158.23, places=5)
        self.assertEqual(s["max_loss"], round(s["qty"] * 0.15 / 158.23, 2))
        self.assertLessEqual(s["max_loss"], 50.0)

    def test_a_cross_needs_the_quote_currency_rate(self):
        plan = {"symbol": "EURGBP", "side": "buy", "date": "2026-11-10", "entry": 0.83565, "stop": 0.83465,
                "target": 0.83765}
        with self.assertRaises(size.SizeError) as cm:
            size.size_market_trade(plan, LEV, today="2026-11-10")
        self.assertIn("GBPUSD", str(cm.exception))
        s = size.size_market_trade(plan, LEV, today="2026-11-10", quote_usd=1.34)
        self.assertEqual(s["qty"], 37313)                                  # $50 / (0.00100 x 1.34)
        s2 = size.size_market_trade(plan, LEV, today="2026-11-10", prices={"GBPUSD": 1.34})
        self.assertEqual(s2["qty"], s["qty"])

    def test_a_short_needs_allow_short(self):
        with self.assertRaises(size.SizeError):
            size.size_market_trade(GOLD_SHORT, {"budget": 10000}, today="2026-11-10")
        s = size.size_market_trade(GOLD_SHORT, GCFG, today="2026-11-10")
        self.assertEqual((s["qty"], s["side"], s["max_loss"], s["cost"]), (1, "sell", 23.4, 4052.2))

    def test_an_ounce_of_gold_does_not_fit_a_small_account_without_leverage(self):
        s = size.size_market_trade(GOLD_SHORT, dict(CFG, allow_short=True), today="2026-11-10")
        self.assertEqual((s["qty"], s["bound_by"]), (0, "position cap"))
        self.assertIn("1 ounce of XAUUSD is worth $4,052.20", s["refusal"])

    def test_gold_stop_too_wide_for_the_max_loss_is_refused(self):
        s = size.size_market_trade(dict(GOLD_SHORT, stop=4110.0, target=3990.0),
                                   GCFG, today="2026-11-10")
        self.assertEqual(s["qty"], 0)
        self.assertIn("more than your $50 max loss", s["refusal"])
        self.assertIsNone(size.market_order(s))

    def test_the_order_keeps_broker_precision(self):
        o = size.market_order(size.size_market_trade(EUR_PLAN, CFG, today="2026-11-09"))
        self.assertEqual((o["qty"], o["stop_loss_stop_price"], o["take_profit_limit_price"], o["market"]),
                         ("3347", "1.11976", "1.12086", "forex"))
        self.assertEqual(o["plan"]["quote_usd"], 1.0)

    def test_a_stale_plan_date_is_refused(self):
        with self.assertRaises(size.SizeError):
            size.size_market_trade(EUR_PLAN, CFG, today="2026-11-10")


def order_for(plan, cfg, today):
    return size.market_order(size.size_market_trade(plan, cfg, today=today))


class Risk(unittest.TestCase):
    NOW = check.parse_now("2026-11-09 08:56")

    def one(self, order, cfg, now=None, **kw):
        return check.check([order], cfg, now=now or self.NOW, **kw)["results"][0]

    def test_a_sized_order_passes(self):
        r = self.one(order_for(EUR_PLAN, CFG, "2026-11-09"), CFG)
        self.assertEqual((r["verdict"], r["kind"], r["market"], r["max_loss"], r["reward_risk"]),
                         ("PASS", "entry", "forex", 1.47, 1.5))

    def test_a_leveraged_order_is_blocked_when_margin_is_off(self):
        r = self.one(order_for(EUR_PLAN, LEV, "2026-11-09"), CFG)
        self.assertEqual(r["verdict"], "BLOCK")
        text = " ".join(r["reasons"])
        self.assertIn("leverage is off", text)
        self.assertIn("max order", text)
        self.assertEqual(self.one(order_for(EUR_PLAN, LEV, "2026-11-09"), LEV)["verdict"], "PASS")

    def test_the_weekend_and_the_late_session_are_blocked(self):
        o = order_for(EUR_PLAN, CFG, "2026-11-09")
        r = self.one(o, CFG, now=check.parse_now("2026-11-07 10:00"))
        self.assertIn("forex market is closed today", " ".join(r["reasons"]))
        r = self.one(o, CFG, now=check.parse_now("2026-11-09 16:10"))
        self.assertIn("no new forex entries after 15:30", " ".join(r["reasons"]))
        r = self.one(o, CFG, now=check.parse_now("2026-11-09 06:00"))
        self.assertIn("outside the New York session", " ".join(r["reasons"]))

    def test_a_london_plan_is_checked_against_the_london_session(self):
        o = order_for(dict(EUR_PLAN, session="london"), CFG, "2026-11-09")
        self.assertEqual(self.one(o, CFG, now=check.parse_now("2026-11-09 04:00"))["verdict"], "PASS")
        r = self.one(o, CFG, now=check.parse_now("2026-11-09 11:45"))
        self.assertIn("outside the London session", " ".join(r["reasons"]))

    def test_a_short_is_blocked_unless_turned_on(self):
        on = GCFG
        o = order_for(GOLD_SHORT, on, "2026-11-10")
        now = check.parse_now("2026-11-10 08:36")
        r = self.one(o, {"budget": 10000, "max_order_usd": 8000}, now=now)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("short selling is off", " ".join(r["reasons"]))
        r = self.one(o, on, now=now)
        self.assertEqual((r["verdict"], r["max_loss"]), ("PASS", 23.4))
        self.assertIn("COMEX gold futures", " ".join(r["warnings"]))

    def test_too_many_units_for_the_max_loss_is_blocked(self):
        o = dict(order_for(dict(EUR_PLAN, stop=1.1152, target=1.1302), LEV, "2026-11-09"), qty="20000")
        r = self.one(o, LEV)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("over your $50 max loss", " ".join(r["reasons"]))

    def test_no_stop_is_blocked(self):
        o = order_for(EUR_PLAN, CFG, "2026-11-09")
        del o["stop_loss_stop_price"]
        o["plan"] = dict(o["plan"], stop=None)
        self.assertIn("stop: no stop", " ".join(self.one(o, CFG)["reasons"]))

    def test_closing_a_recorded_position_is_an_exit_and_never_blocked(self):
        pf = {"cash": 5000, "positions": [], "margin_positions": [{"symbol": "EURUSD", "units": 3347, "avg_price": 1.1202}]}
        o = {"symbol": "EURUSD", "side": "sell", "qty": "3347", "type": "market", "time_in_force": "day"}
        r = self.one(o, CFG, now=check.parse_now("2026-11-09 16:55"), portfolio=pf)
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"))

    def test_the_daily_loss_limit_counts_forex_and_stocks_together(self):
        state = {"orders": [{"status": "placed", "date": "2026-11-09", "symbol": "TSLA", "side": "buy",
                             "kind": "entry", "max_loss": 120.0}]}
        o = order_for(dict(EUR_PLAN, stop=1.1152, target=1.1302), LEV, "2026-11-09")     # max loss $50
        r = self.one(o, LEV, state=state)
        self.assertIn("daily loss", " ".join(r["reasons"]))

    def test_stock_orders_are_untouched(self):
        o = {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32",
             "plan": {"entry": 364.18, "stop": 358.32, "target": 370.04}}
        r = check.check([o], CFG, now=check.parse_now("2026-10-19 10:15"))["results"][0]
        self.assertEqual((r["verdict"], r["max_loss"]), ("PASS", 46.88))
        self.assertNotIn("market", r)


class Ticket(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.pf = os.path.join(self.tmp, "portfolio.json")

    def test_plain_ticket_and_the_oanda_order(self):
        o = order_for(EUR_PLAN, LEV, "2026-11-09")
        text = ticket.plain(o)
        self.assertIn("BUY 66,952 units of EURUSD (EUR/USD), market", text)
        self.assertIn("stop-loss 1.11976 (4.4 pips)", text)
        self.assertIn("max loss about $29.46", text)
        self.assertEqual(ticket.oanda_body(o), {"order": {
            "type": "MARKET", "instrument": "EUR_USD", "units": "66952", "timeInForce": "FOK",
            "positionFill": "DEFAULT", "stopLossOnFill": {"price": "1.11976", "timeInForce": "GTC"},
            "takeProfitOnFill": {"price": "1.12086", "timeInForce": "GTC"}}})

    def test_a_sell_has_negative_units(self):
        plan = dict(EUR_PLAN, side="sell", entry=1.11857, stop=1.12152, target=1.11562, date="2026-11-10")
        o = order_for(plan, LEV, "2026-11-10")
        self.assertTrue(ticket.oanda_body(o)["order"]["units"].startswith("-"))

    def test_fingerprint_matches_the_risk_check(self):
        o = order_for(EUR_PLAN, CFG, "2026-11-09")
        self.assertEqual(ticket.fingerprint(o), check.fingerprint(o))

    def test_a_metal_ticket_gives_distances_not_futures_prices(self):
        o = order_for(GOLD_SHORT, GCFG, "2026-11-10")
        text = "\n".join(ticket.market_how(o, GCFG))
        self.assertIn("stop $23.40 above your fill", text)
        self.assertIn("take-profit $23.40 below your fill", text)
        self.assertNotIn("POST /v3", text)

    def test_yes_rules_still_hold(self):
        o = order_for(EUR_PLAN, CFG, "2026-11-09")
        self.assertTrue(ticket.parse_yes("yes", o)[0])
        self.assertTrue(ticket.parse_yes("yes, 3347 EURUSD", o)[0])
        self.assertFalse(ticket.parse_yes("yes to all", o)[0])
        self.assertFalse(ticket.parse_yes("yes 5000", o)[0])
        self.assertFalse(ticket.parse_yes("ok", o)[0])

    def test_a_round_trip_books_only_the_profit(self):
        cfg = {"budget": 5000}
        pf, pnl = ticket.apply_market_fill(self.pf, cfg, "EURUSD", "buy", 66952, 1.12022, "2026-11-09")
        self.assertIsNone(pnl)
        self.assertEqual(pf["cash"], 5000.0)                               # margin product: cash is not spent
        self.assertEqual(pf["margin_positions"][0]["units"], 66952.0)
        pf, pnl = ticket.apply_market_fill(self.pf, cfg, "EURUSD", "sell", 66952, 1.12086, "2026-11-09")
        self.assertEqual((pnl, pf["cash"]), (42.85, 5042.85))
        self.assertNotIn("margin_positions", pf)

    def test_a_short_round_trip_and_a_yen_conversion(self):
        cfg = {"budget": 5000}
        ticket.apply_market_fill(self.pf, cfg, "USDJPY", "sell", 10000, 158.38, "2026-11-10")
        with open(self.pf) as fh:
            self.assertEqual(json.load(fh)["margin_positions"][0]["units"], -10000.0)
        pf, pnl = ticket.apply_market_fill(self.pf, cfg, "USDJPY", "buy", 10000, 158.68, "2026-11-10")
        self.assertEqual(pnl, round(10000 * -0.30 / 158.68, 2))            # a short loses when the price rises
        self.assertEqual(pf["cash"], round(5000 + pnl, 2))

    def test_a_cross_needs_the_rate_to_book_a_close(self):
        cfg = {"budget": 5000}
        ticket.apply_market_fill(self.pf, cfg, "EURGBP", "buy", 1000, 0.8356, "2026-11-10")
        with self.assertRaises(ticket.TicketError):
            ticket.apply_market_fill(self.pf, cfg, "EURGBP", "sell", 1000, 0.8366, "2026-11-10")
        pf, pnl = ticket.apply_market_fill(self.pf, cfg, "EURGBP", "sell", 1000, 0.8366, "2026-11-10", 1.34)
        self.assertEqual(pnl, 1.34)


class Chain(unittest.TestCase):
    """entry -> exit -> budget -> risk -> ticket, through the command lines, on the real EURUSD session."""

    def test_the_whole_order_path(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        f = lambda name: os.path.join(tmp, name)
        with open(os.path.join(os.path.dirname(FIX), "..", "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        with open(f("config.json"), "w") as fh:
            json.dump(cfg, fh)
        code, _, _ = run(entry, ["check", "EURUSD", "--now", "2026-11-09 08:55", "--offline", EUR,
                                 "--config", f("config.json"), "--write", f("entry.json")])
        self.assertEqual(code, 0)
        code, _, _ = run(exitp, ["plan", "--from", f("entry.json"), "--target", "1.5R", "--now", "2026-11-09 08:55",
                                 "--config", f("config.json"), "--write", f("plan.json")])
        self.assertEqual(code, 0)
        code, text, _ = run(size, ["--plan", f("plan.json"), "--config", f("config.json"), "--portfolio", f("pf.json"),
                                   "--now", "2026-11-09 08:55", "--write", f("orders.json")])
        self.assertEqual(code, 0)
        self.assertIn("Size: 3,347 units of EURUSD", text)
        self.assertIn("No leverage", text)
        code, text, _ = run(check, ["--orders", f("orders.json"), "--config", f("config.json"),
                                    "--portfolio", f("pf.json"), "--prices", f("prices.json"),
                                    "--state", f("broker.json"), "--out", f("risk.json"), "--now", "2026-11-09 08:56"])
        self.assertEqual(code, 0, text)
        common = ["--orders", f("orders.json"), "--config", f("config.json"), "--risk", f("risk.json"),
                  "--state", f("broker.json"), "--now", "2026-11-09 08:57"]
        code, text, _ = run(ticket, ["show"] + common)
        self.assertEqual(code, 0)
        self.assertIn("READY", text)
        self.assertIn('"instrument": "EUR_USD"', text)
        self.assertNotIn("place_stock_order", text)
        code, _, _ = run(ticket, ["approve", "--index", "1", "--reply", "sure"] + common)
        self.assertEqual(code, 1)
        code, _, _ = run(ticket, ["approve", "--index", "1", "--reply", "yes"] + common)
        self.assertEqual(code, 0)
        code, _, _ = run(ticket, ["result", "--index", "1", "--status", "placed", "--filled-qty", "3347",
                                  "--filled-price", "1.12021", "--portfolio", f("pf.json")] + common)
        self.assertEqual(code, 0)
        code, text, _ = run(ticket, ["flatten", "--symbol", "EURUSD", "--orders", f("orders.json"),
                                     "--portfolio", f("pf.json"), "--plan", f("plan.json"),
                                     "--config", f("config.json"), "--now", "2026-11-09 16:55"])
        self.assertEqual(code, 0)
        self.assertIn("SELL 3,347 units of EURUSD", text)


class Alerts(unittest.TestCase):
    def ring(self, rules, quotes, now):
        utc = alerts.parse_now(now)
        return alerts.evaluate(rules, quotes, alerts.market_status(utc), {}, utc)[0]

    def test_a_long_plan_rings_below_the_stop_and_above_the_target(self):
        rules = alerts.rules_from_plan(EUR_PLAN)
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("eurusd-plan-stop", "stop"), ("eurusd-plan-target", "target"), ("eurusd-flat-by", "time")])
        self.assertEqual(rules[2]["at"], "16:55")
        hit = self.ring(rules, {"EURUSD": {"price": 1.1197}}, "2026-11-09 10:00")
        self.assertEqual([r["id"] for r in hit], ["eurusd-plan-stop"])
        self.assertIn("EURUSD is 1.11970: at or below your stop 1.11976", hit[0]["message"])
        hit = self.ring(rules, {"EURUSD": {"price": 1.1209}}, "2026-11-09 10:00")
        self.assertEqual([r["id"] for r in hit], ["eurusd-plan-target"])

    def test_a_short_plan_is_mirrored(self):
        rules = alerts.rules_from_plan(GOLD_SHORT)
        hit = self.ring(rules, {"XAUUSD": {"price": 4080.0}}, "2026-11-10 10:00")
        self.assertEqual([r["id"] for r in hit], ["xauusd-plan-stop"])
        self.assertIn("at or above your stop $4075.60", hit[0]["message"])
        hit = self.ring(rules, {"XAUUSD": {"price": 4020.0}}, "2026-11-10 10:00")
        self.assertEqual([r["id"] for r in hit], ["xauusd-plan-target"])
        self.assertEqual(self.ring(rules, {"XAUUSD": {"price": 4050.0}}, "2026-11-10 10:00"), [])

    def test_flat_by_rings_before_the_rollover_even_on_a_stock_holiday(self):
        plan = dict(EUR_PLAN, date="2026-11-26")                           # Thanksgiving: the NYSE is closed
        hit = self.ring(alerts.rules_from_plan(plan), {"EURUSD": {"price": 1.1203}}, "2026-11-26 16:56")
        self.assertEqual([r["id"] for r in hit], ["eurusd-flat-by"])
        self.assertIn("rolls over at 17:00", hit[0]["message"])
        self.assertIn("flatten --symbol EURUSD", hit[0]["message"])

    def test_quotes_ask_yahoo_for_the_right_symbol(self):
        self.assertTrue(alerts.yahoo_url("EUR/USD").endswith("/EURUSD=X?range=1d&interval=1d"))
        self.assertTrue(alerts.yahoo_url("XAUUSD").endswith("/GC=F?range=1d&interval=1d"))
        self.assertTrue(alerts.yahoo_url("BRK.B").endswith("/BRK-B?range=1d&interval=1d"))

    def test_the_forex_week(self):
        f = lambda text: alerts.fx_open(alerts.to_eastern(alerts.parse_now(text)))
        self.assertTrue(f("2026-11-09 03:00"))
        self.assertFalse(f("2026-11-07 12:00"))      # Saturday
        self.assertFalse(f("2026-11-08 16:00"))      # Sunday before the 17:00 open
        self.assertTrue(f("2026-11-08 17:30"))
        self.assertFalse(f("2026-11-13 17:30"))      # Friday after the close


class TimeMachine(unittest.TestCase):
    def test_it_says_plainly_that_it_cannot_replay_forex_or_metals(self):
        for plan in (EUR_PLAN, GOLD_SHORT, {"symbol": "EUR/USD", "date": "2026-11-09", "entry": 1.1, "stop": 1.0,
                                           "target": 1.2}):
            with self.assertRaises(tm.TMError) as cm:
                tm.normalize_plan(dict(plan))
            self.assertIn("NYSE sessions only", str(cm.exception))


class Defaults(unittest.TestCase):
    def test_the_example_config_keeps_leverage_and_shorts_off(self):
        with open(os.path.join(os.path.dirname(FIX), "..", "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["allow_margin"], False)
        self.assertIs(cfg["allow_short"], False)
        self.assertEqual(sorted(cfg["markets"]), ["forex", "metals"])
        self.assertEqual(size.leverage_of(cfg, size.instrument("EURUSD")), 1.0)


if __name__ == "__main__":
    unittest.main()
