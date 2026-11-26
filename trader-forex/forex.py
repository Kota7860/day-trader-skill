#!/usr/bin/env python3
"""Forex Market for trader-forex: the currency pairs, gold and silver, the four
trading sessions, and a session-breakout scan across every pair. Standard
library only. It never places an order.

    python3 forex.py pairs                          # the universe: majors, crosses, metals
    python3 forex.py sessions                       # Sydney, Tokyo, London, New York: open or not
    python3 forex.py scan                           # New York session breakouts, every pair
    python3 forex.py scan --session london --group majors
    python3 forex.py scan --now "2026-11-09 08:55" --offline DIR

The pairs play the part Nasdaq Trader's symbol list plays for stocks: a fixed,
known universe. A scan reads each pair's free Yahoo 5-minute bars, builds the
session's opening range (the first 30 minutes by default) and lists every
pair whose bar has CLOSED above the range high (a buy) or below the range low
(a sell), the same rule trader-entry uses. Hits go to trader/watchlist.json for
trader-entry. A sell is listed for information; trading it needs
"allow_short": true in trader/config.json.

All times are New York time. Gold and silver are COMEX futures (GC=F, SI=F),
about 10 minutes delayed. Yahoo's forex bars are indicative mid prices.

Exit codes: scan 0 when at least one pair broke out, 1 otherwise.
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PAUSE = 0.3

MAJORS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
CROSSES = ["EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "GBPJPY", "GBPCHF", "GBPAUD",
           "AUDJPY", "AUDNZD", "CADJPY", "CHFJPY", "NZDJPY", "AUDCAD"]
METALS = ["XAUUSD", "XAGUSD"]
GROUPS = {"majors": MAJORS, "crosses": CROSSES, "metals": METALS, "all": MAJORS + CROSSES + METALS}

# local open and close of each session, in that city's own clock
CITY_SESSIONS = [("Sydney", "sydney", datetime.time(7, 0), datetime.time(16, 0)),
                 ("Tokyo", "tokyo", datetime.time(9, 0), datetime.time(18, 0)),
                 ("London", "london", datetime.time(8, 0), datetime.time(16, 30)),
                 ("New York", "ny", datetime.time(8, 0), datetime.time(17, 0))]


class ForexError(Exception):
    pass


def _load_entry():
    """trader-entry's entry.py, the sibling folder: one rule for a breakout, not two."""
    path = os.path.join(os.path.dirname(HERE), "trader-entry", "entry.py")
    if not os.path.exists(path):
        raise ForexError("trader-forex needs the trader-entry skill next to it (%s): install the whole pack" % path)
    spec = importlib.util.spec_from_file_location("trader_entry_for_forex", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


entry = _load_entry()


# ---------------------------------------------------------------- clocks

def _first_sunday(year, month):
    d = datetime.date(year, month, 1)
    return d + datetime.timedelta(days=(6 - d.weekday()) % 7)


def city_offset(city, utc):
    """Hours from UTC for a session's city at that moment."""
    if city == "tokyo":
        return 9
    if city == "london":
        return entry.london_offset(utc)
    if city == "ny":
        return entry.eastern_offset(utc)
    # Sydney: daylight time from 2:00 standard on the first Sunday of October to
    # 3:00 daylight (16:00 UTC the day before) on the first Sunday of April
    y = utc.year
    end = datetime.datetime.combine(_first_sunday(y, 4), datetime.time(0, 0)) - datetime.timedelta(hours=8)
    start = datetime.datetime.combine(_first_sunday(y, 10), datetime.time(0, 0)) - datetime.timedelta(hours=8)
    u = utc.replace(tzinfo=None)
    return 11 if (u < end or u >= start) else 10


def city_to_utc(city, local):
    for off in (11, 10, 9, 1, 0, -4, -5):
        utc = local - datetime.timedelta(hours=off)
        if city_offset(city, utc) == off:
            return utc
    return local


def _fx_open(local):
    wd, t = local.weekday(), local.time()
    if wd == 5 or (wd == 6 and t < datetime.time(17, 0)) or (wd == 4 and t >= datetime.time(17, 0)):
        return False
    return local.strftime("%m-%d") not in ("12-25", "01-01")


def sessions_at(now_utc):
    """Each session today (in its own city's date) as New York times, and whether it is open now."""
    out = []
    market = _fx_open(entry.to_eastern(now_utc))
    for name, key, o, c in CITY_SESSIONS:
        local_now = now_utc + datetime.timedelta(hours=city_offset(key, now_utc))
        rows = []
        for back in (-1, 0, 1):
            d = local_now.date() + datetime.timedelta(days=back)
            if d.weekday() >= 5:
                continue
            ou = city_to_utc(key, datetime.datetime.combine(d, o))
            cu = city_to_utc(key, datetime.datetime.combine(d, c))
            rows.append((ou, cu))
        current = [r for r in rows if r[0] <= now_utc < r[1]]
        upcoming = [r for r in rows if r[0] > now_utc]
        pick = current[0] if current else (upcoming[0] if upcoming else None)
        item = {"session": name, "key": key, "open": bool(current) and market}
        if pick:
            item["opens_ny"] = entry.to_eastern(pick[0]).strftime("%a %H:%M")
            item["closes_ny"] = entry.to_eastern(pick[1]).strftime("%a %H:%M")
            mins = int(((pick[1] if current else pick[0]) - now_utc).total_seconds() // 60)
            item["minutes"] = mins
        out.append(item)
    return out, market


# ---------------------------------------------------------------- the scan

def offline_chart(folder, sym):
    inst = entry.instrument(sym)
    for name in (inst["yahoo"] + ".json", inst["yahoo"] + ".json.gz", inst["yahoo"] + "-5d5m.json.gz",
                 inst["symbol"] + ".json", inst["symbol"] + ".json.gz"):
        p = os.path.join(folder, name)
        if os.path.exists(p):
            return entry.get_chart(sym, "5m", offline=p)
    raise ForexError("no offline bars for %s in %s" % (inst["symbol"], folder))


def scan(symbols, now_utc, session="ny", or_minutes=30, offline=None, cfg=None, sleep=time.sleep):
    """{hits, waiting, failed}. Each pair is read once and judged both ways: a close above
    the range high (buy) and a close below the range low (sell)."""
    hits, waiting, failed, closed = [], [], [], None
    today = entry.to_eastern(entry.utc_now()).date()
    day = entry.to_eastern(now_utc).date()
    for i, sym in enumerate(symbols):
        inst = entry.instrument(sym)
        try:
            if offline:
                chart = offline_chart(offline, sym)
            else:
                if i:
                    sleep(PAUSE)
                chart = entry.get_chart(sym, "5m", day, None, today)
        except (entry.EntryError, ForexError) as e:
            failed.append({"symbol": inst["symbol"], "error": str(e)})
            continue
        best, near = None, None
        for side in ("buy", "sell"):
            r = entry.evaluate(chart, now_utc, setup="orb", or_minutes=or_minutes, interval="5m", cfg=cfg,
                               side=side, session_name=session, symbol=inst["symbol"])
            if r["phase"] == "closed":
                closed = r["reason"]
            if r["status"] == "triggered":
                t = r["trigger"]
                if best is None or t["close_time"] < best["time_24"]:
                    best = {"symbol": inst["symbol"], "market": inst["market"], "side": side,
                            "session": session, "time": t["time"], "time_24": t["close_time"],
                            "price": t["price"], "level": r["level"], "beyond": entry.beyond_txt(inst, t["close"], r["level"]),
                            "range": r["opening_range"], "last": (r.get("last") or {}).get("close"),
                            "decimals": inst["decimals"]}
            elif side == "buy" and r["status"] == "waiting" and r.get("opening_range") and r["opening_range"]["complete"]:
                last = (r.get("last") or {}).get("close")
                orr = r["opening_range"]
                if last is not None:
                    gap_hi, gap_lo = orr["high"] - last, last - orr["low"]
                    near = ({"symbol": inst["symbol"], "last": last, "high": orr["high"], "low": orr["low"],
                                    "nearest": "high" if gap_hi <= gap_lo else "low",
                                    "away": (entry.beyond_txt(inst, last, orr["high"]) if gap_hi <= gap_lo
                                             else entry.beyond_txt(inst, last, orr["low"])),
                                    "rank": min(gap_hi, gap_lo) / last, "decimals": inst["decimals"]})
        if best:
            hits.append(best)
        elif near:
            waiting.append(near)
    hits.sort(key=lambda h: (h["time_24"], h["symbol"]))
    waiting.sort(key=lambda w: w["rank"])
    return {"hits": hits, "waiting": waiting, "failed": failed, "closed": closed}


def px(dec, x):
    return "%.*f" % (dec, x)


def watchlist_doc(res, now_utc, session, or_minutes, universe):
    return {"created_by": "trader-forex", "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": "session-orb", "market": "forex", "session": session, "or_minutes": or_minutes,
            "as_of": entry.to_eastern(now_utc).strftime("%Y-%m-%d %H:%M ET"), "universe": universe,
            "scanned": universe - len(res["failed"]), "failed": len(res["failed"]), "source": "yahoo",
            "note": "A list of charts that matched a rule, not trades to take. Not financial advice.",
            "hits": [{k: h[k] for k in ("symbol", "market", "side", "session", "time", "price", "level")}
                     for h in res["hits"]]}


# ---------------------------------------------------------------- commands

def cmd_pairs(a):
    rows = []
    for g in ("majors", "crosses", "metals"):
        for s in GROUPS[g]:
            i = entry.instrument(s)
            rows.append({"symbol": s, "group": g, "name": i["name"], "yahoo": i["yahoo"], "decimals": i["decimals"],
                         "pip": i["pip"], "quote": i["quote"]})
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    print("%-8s %-8s %-9s %-7s %-8s %s" % ("SYMBOL", "GROUP", "NAME", "YAHOO", "DECIMALS", "PIP"))
    for r in rows:
        print("%-8s %-8s %-9s %-7s %-8d %s" % (r["symbol"], r["group"], r["name"], r["yahoo"], r["decimals"],
                                              ("%g" % r["pip"]) if r["pip"] else "- (dollars)"))
    print("%d pairs and metals. Write a pair any usual way: EURUSD, EUR/USD, eur_usd." % len(rows))
    print("Gold and silver read COMEX futures (GC=F, SI=F), about 10 minutes delayed.")
    return 0


def cmd_sessions(a):
    now = entry.parse_now(a.now)
    rows, market = sessions_at(now)
    if a.json:
        print(json.dumps({"now_new_york": entry.to_eastern(now).strftime("%Y-%m-%d %H:%M"), "market_open": market,
                          "sessions": rows}, indent=2))
        return 0 if market else 1
    local = entry.to_eastern(now)
    print("Forex market %s, %s New York." % ("OPEN" if market else "CLOSED", local.strftime("%a %Y-%m-%d %H:%M")))
    if not market:
        print("The forex week runs Sunday 17:00 to Friday 17:00 New York (closed December 25 and January 1).")
    for r in rows:
        if "opens_ny" not in r:
            continue
        h, m = divmod(max(r.get("minutes", 0), 0), 60)
        left = "%dh%02d" % (h, m)
        state = ("open, closes in %s" % left) if r["open"] else ("opens in %s" % left)
        print("  %-9s %-28s %s to %s New York" % (r["session"], state, r["opens_ny"], r["closes_ny"]))
    both = [r["session"] for r in rows if r["open"]]
    if len(both) > 1:
        print("Overlap now: %s. Spreads are usually tightest and moves biggest in an overlap." % " and ".join(both))
    print("The scan and trader-entry use the London or the New York session (--session london|ny).")
    return 0 if market else 1


def cmd_scan(a):
    now = entry.parse_now(a.now)
    if a.session not in ("ny", "london"):
        raise ForexError("--session is ny or london")
    if a.symbols:
        syms = [entry.instrument(s)["symbol"] for s in a.symbols.replace(",", " ").split()]
        bad = [s for s in syms if entry.instrument(s)["market"] == "stock"]
        if bad:
            raise ForexError("not a pair or a metal: %s (stocks go to /trader-scanner)" % ", ".join(bad))
    else:
        syms = GROUPS[a.group]
    cfg = entry.read_config(a.config)
    res = scan(syms, now, a.session, a.or_minutes, a.offline, cfg)
    doc = watchlist_doc(res, now, a.session, a.or_minutes, len(syms))
    if not a.no_write and res["hits"]:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(doc, waiting=res["waiting"], errors=res["failed"]), indent=2))
        return 0 if res["hits"] else 1
    name = {"ny": "New York", "london": "London"}[a.session]
    print("%s-session breakout scan, %s ET: %d-minute range, 5-minute bars, %d pairs (Yahoo, free, can lag)."
          % (name, entry.to_eastern(now).strftime("%a %Y-%m-%d %H:%M"), a.or_minutes, len(syms)))
    if res["closed"] and not res["hits"]:
        print(res["closed"][:1].upper() + res["closed"][1:] + ".")
    if res["hits"]:
        print("")
        print("%-7s %-5s %-9s %-11s %-11s %s" % ("PAIR", "SIDE", "TIME", "PRICE", "LEVEL", "BEYOND"))
        for h in res["hits"]:
            print("%-7s %-5s %-9s %-11s %-11s %s" % (h["symbol"], h["side"], h["time"], px(h["decimals"], h["price"]),
                                                    px(h["decimals"], h["level"]), h["beyond"]))
    else:
        print("No pair has closed beyond its range yet.")
    if res["waiting"]:
        print("")
        print("Closest to a breakout:")
        for w in res["waiting"][:a.show]:
            print("  %-7s last %-10s range %s to %s, %s from the %s" % (
                w["symbol"], px(w["decimals"], w["last"]), px(w["decimals"], w["low"]), px(w["decimals"], w["high"]),
                w["away"], w["nearest"]))
    if len(res["failed"]) > 3:
        print("Could not read %d of %d: %s. First reason: %s" % (
            len(res["failed"]), len(syms), ", ".join(f["symbol"] for f in res["failed"]), res["failed"][0]["error"]))
    else:
        for f in res["failed"]:
            print("  could not read %s: %s" % (f["symbol"], f["error"]))
    if res["hits"] and not a.no_write:
        print("")
        print("Wrote %d hit(s) to %s." % (len(res["hits"]), a.out))
        print("Next: /trader-entry checks one (entry.py check EURUSD --session %s), then /trader-exit." % a.session)
        if any(h["side"] == "sell" for h in res["hits"]) and not cfg.get("allow_short"):
            print("A sell hit is information only: trading it needs \"allow_short\": true in trader/config.json.")
    print("A breakout on a chart is not a prediction. Not financial advice. Nothing was placed.")
    return 0 if res["hits"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Forex Market (trader-forex): pairs, sessions and a breakout scan.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("pairs", help="the pairs and metals this pack trades")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("sessions", help="which trading sessions are open")
    p.add_argument("--now", help='pretend it is this New York time, e.g. "2026-11-09 08:55"')
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("scan", help="session-range breakouts across the pairs")
    p.add_argument("--session", default="ny", help="ny (default) or london")
    p.add_argument("--group", default="all", choices=sorted(GROUPS))
    p.add_argument("--symbols", help="your own list: EURUSD,GBPJPY,XAUUSD")
    p.add_argument("--or-minutes", type=int, default=30)
    p.add_argument("--now", help='pretend it is this New York time, e.g. "2026-11-09 08:55"')
    p.add_argument("--offline", metavar="DIR", help="a folder of saved Yahoo chart JSON (EURUSD=X.json ...)")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--out", default="trader/watchlist.json")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--show", type=int, default=5, help="how many near-breakouts to list")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"pairs": cmd_pairs, "sessions": cmd_sessions, "scan": cmd_scan}[a.cmd](a)
    except (ForexError, entry.EntryError) as e:
        print("forex.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
