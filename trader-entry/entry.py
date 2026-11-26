#!/usr/bin/env python3
"""Entry Timer for trader-entry: watches a setup bar by bar and says the
moment to get in. Standard library only. Stocks are long only. It never places an order.

Forex pairs (EURUSD, EUR/USD) and metals (XAUUSD gold, XAGUSD silver) work too:
the range is the first 30 minutes of the New York session (8:00 to 17:00 New
York) or of --session london, prices keep the market's decimals, and --side sell
watches for a close BELOW the range low when "allow_short" is true in the
config. Gold and silver read COMEX futures (GC=F, SI=F), about 10 minutes delayed.

    python3 entry.py check EURUSD --session london
    python3 entry.py check XAUUSD --side sell

    python3 entry.py check TSLA                          # opening-range breakout, 5-minute bars
    python3 entry.py check TSLA --now "2026-10-19 10:15" --offline TSLA.json
    python3 entry.py check TSLA --setup level --level 371.20    # first close above a price
    python3 entry.py check TSLA --interval 1m --min-volume-x 1.5 --write
    python3 entry.py watch TSLA --every 30 --until 15:30  # one line per closed bar
    python3 entry.py watchlist                            # every hit in trader/watchlist.json

Setups (v2 plans longs only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 9:30, 9:35 and 9:40 five-minute bars).
         It triggers on the first COMPLETED bar after the range that CLOSES
         above the range high. A wick above the high is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from trader-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars (the 9:30 bar carries the opening
         auction, so 1.0 is already a high bar).

A trigger's time is the trigger bar's CLOSE time (the 10:10 bar closes at
10:15 AM) and its price is that close, to the cent. Only bars on the interval
grid count (Yahoo's live tick, e.g. 10:49:45, is dropped) and a bar counts only
once it has closed: now >= its start + the interval.

Statuses: triggered; waiting (not open yet, range still forming, or no close
above the level yet); expired (no trigger by no_entries_after, default 15:30;
on a 1:00 p.m. early close the cutoff moves up three hours with the close);
no-data (the market is closed today or Yahoo sent no bars).

trader/config.json (optional): "no_entries_before" (09:45) and
"no_entries_after" (15:30), New York time.

watch wakes just after each bar closes (bars only change then), re-polls every
--every seconds (default 30) while Yahoo has not posted the new bar, prints one
line per closed bar and stops on a trigger (exit 0, writes trader/entry.json),
on expiry, at the close or at --until (exit 1). With --now or --offline it runs
on a simulated clock and replays the day without sleeping.

Data: Yahoo's free chart JSON (unofficial, can lag, bars not single trades)
  https://query2.finance.yahoo.com/v8/finance/chart/TSLA?range=1d&interval=5m
  falls back to query1 on HTTP 429 or 5xx; a 429 gets one retry after a pause.
  --offline PATH reads a saved chart JSON (.json or .json.gz) instead, or
  PATH/<TICKER>.json when PATH is a folder. Without --now, an offline file is
  read as of its own time (meta.regularMarketTime).

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5      # seconds between requests to the free endpoint
BACKOFF = 5.0    # seconds before the one retry after an HTTP 429
GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Yahoo's data to catch up
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": "09:45", "no_entries_after": "15:30"}
EPOCH = datetime.datetime(1970, 1, 1)

NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
CALENDAR_YEARS = (2026, 2027)
OPEN = datetime.time(9, 30)
CLOSE = datetime.time(16, 0)
EARLY = datetime.time(13, 0)


class EntryError(Exception):
    pass


# ---------------------------------------------------------------- markets
# Stocks keep the NYSE session and cents. Forex pairs (EURUSD, EUR/USD) and
# metals (XAUUSD gold, XAGUSD silver) trade around the clock on weekdays, so
# their "open" is a named session, and their prices carry more decimals.

CCYS = ("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK", "DKK", "MXN", "ZAR",
        "SGD", "HKD", "CNH", "TRY", "PLN", "INR")
METALS = {"XAUUSD": {"yahoo": "GC=F", "name": "gold", "decimals": 2},
          "XAGUSD": {"yahoo": "SI=F", "name": "silver", "decimals": 3}}
METAL_ALIASES = {"XAUUSD": "XAUUSD", "XAU": "XAUUSD", "GC=F": "XAUUSD",
                 "XAGUSD": "XAGUSD", "XAG": "XAGUSD", "SI=F": "XAGUSD"}
SESSIONS = {"ny": ("New York", datetime.time(8, 0), datetime.time(17, 0)),
            "london": ("London", datetime.time(8, 0), datetime.time(16, 30))}
FX_CLOSED = {"01-01": "New Year's Day", "12-25": "Christmas Day"}
METAL_CLOSED = {"2026-04-03": "Good Friday", "2027-03-26": "Good Friday"}


def instrument(sym):
    """What a symbol is: {market: stock | forex | metal, symbol, yahoo, decimals, pip, unit}.
    EURUSD, EUR/USD, EUR_USD and EURUSD=X are one forex pair; XAUUSD, XAU and GC=F are gold."""
    raw = str(sym or "").strip().upper()
    flat = re.sub(r"[/_\-\s]", "", raw)
    if flat.endswith("=X"):
        flat = flat[:-2]
    metal = METAL_ALIASES.get(raw) or METAL_ALIASES.get(flat)
    if metal:
        m = METALS[metal]
        return {"market": "metal", "symbol": metal, "yahoo": m["yahoo"], "decimals": m["decimals"],
                "pip": None, "unit": "ounce", "name": m["name"], "base": metal[:3], "quote": "USD"}
    if len(flat) == 6 and flat.isalpha() and flat[:3] in CCYS and flat[3:] in CCYS and flat[:3] != flat[3:]:
        dec = 3 if flat[3:] == "JPY" else 5
        return {"market": "forex", "symbol": flat, "yahoo": flat + "=X", "decimals": dec,
                "pip": 10.0 ** -(dec - 1), "unit": "unit", "name": flat[:3] + "/" + flat[3:],
                "base": flat[:3], "quote": flat[3:]}
    return {"market": "stock", "symbol": raw, "yahoo": raw.replace(".", "-"), "decimals": 2,
            "pip": None, "unit": "share", "name": raw, "base": raw, "quote": "USD"}


def px(inst, x):
    """A price for a person: $364.18 for a stock or a metal, 1.11371 for a pair."""
    if inst["market"] == "forex":
        return "%.*f" % (inst["decimals"], x)
    return "$%.*f" % (inst["decimals"], x)


def beyond_txt(inst, price, lvl):
    """How far a price is past a level: +0.55% for a stock or a metal, +2.5 pips for a pair."""
    if inst["market"] == "forex":
        return "%+.1f pips" % ((price - lvl) / inst["pip"])
    return "%+.2f%%" % (100.0 * (price / lvl - 1))


def default_or_minutes(inst):
    """15 minutes after the NYSE open; 30 for a session of a 24-hour market, where
    the first quarter hour is too tight a range."""
    return 15 if inst["market"] == "stock" else 30


def market_cfg(cfg, inst):
    """The "markets": {"forex": {...}, "metals": {...}} block of trader/config.json."""
    key = {"forex": "forex", "metal": "metals"}.get(inst["market"])
    m = ((cfg or {}).get("markets") or {}).get(key) if key else None
    return m if isinstance(m, dict) else {}


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise.
    DST runs from 2:00 a.m. on the second Sunday of March to 2:00 a.m. on the
    first Sunday of November (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))   # 2:00 EST
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))    # 2:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text):
    """--now -> naive UTC. "2026-10-19 10:15" (or ...T10:15, or just 10:15 for
    today) is New York time; an offset or Z makes it absolute."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" ET"):
        t = t[:-3].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_eastern(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise EntryError('--now wants a New York time like "2026-10-19 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise EntryError("%s wants a New York time like 15:30, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    """10:10, 09:45: the contract's wall-clock form."""
    return "%02d:%02d" % (t.hour, t.minute)


def short(t):
    """9:45, 10:10: for sentences."""
    return "%d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NYSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NYSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NYSE holiday: " + name
    return True, ""


def session(d):
    """(open_time, close_time) in New York time, or None if closed all day."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, (EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE)


def next_open(d):
    nd = d + datetime.timedelta(days=1)
    for _ in range(15):
        if session(nd):
            return "%s 09:30" % nd.isoformat()
        nd += datetime.timedelta(days=1)
    return None


def _last_sunday(year, month):
    d = datetime.date(year + (month == 12), month % 12 + 1, 1) - datetime.timedelta(days=1)
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)


def london_offset(utc):
    """Hours from UTC for London: +1 from 1:00 UTC on the last Sunday of March to
    1:00 UTC on the last Sunday of October, else 0."""
    y = utc.year
    start = datetime.datetime.combine(_last_sunday(y, 3), datetime.time(1, 0))
    end = datetime.datetime.combine(_last_sunday(y, 10), datetime.time(1, 0))
    return 1 if start <= utc.replace(tzinfo=None) < end else 0


def london_to_eastern(local):
    """A London wall-clock time -> the New York wall-clock time of the same moment."""
    for off in (1, 0):
        utc = local - datetime.timedelta(hours=off)
        if london_offset(utc) == off:
            return to_eastern(utc)
    return to_eastern(local)


def market_day(inst, d):
    """(is_trading_day, reason) for the instrument's market."""
    if inst["market"] == "stock":
        return trading_day(d)
    if d.weekday() >= 5:
        return False, "weekend"
    name = FX_CLOSED.get(d.strftime("%m-%d"))
    if not name and inst["market"] == "metal":
        name = METAL_CLOSED.get(d.isoformat())
    if name:
        return False, "holiday: " + name
    return True, ""


def market_session(inst, d, name="ny"):
    """(open, close) as New York datetimes on day d, or None if closed all day.
    A stock's session is the NYSE's; a pair's or a metal's is the named session."""
    if not market_day(inst, d)[0]:
        return None
    if inst["market"] == "stock":
        o, c = session(d)
        return datetime.datetime.combine(d, o), datetime.datetime.combine(d, c)
    if name not in SESSIONS:
        raise EntryError("--session is %s" % " or ".join(sorted(SESSIONS)))
    _, o, c = SESSIONS[name]
    if name == "london":
        return (london_to_eastern(datetime.datetime.combine(d, o)),
                london_to_eastern(datetime.datetime.combine(d, c)))
    return datetime.datetime.combine(d, o), datetime.datetime.combine(d, c)


def next_market_open(inst, d, name="ny"):
    nd = d + datetime.timedelta(days=1)
    for _ in range(15):
        s = market_session(inst, nd, name)
        if s:
            return s[0].strftime("%Y-%m-%d %H:%M")
        nd += datetime.timedelta(days=1)
    return None


# ---------------------------------------------------------------- Yahoo

def yahoo_symbol(sym):
    return instrument(sym)["yahoo"]


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (yahoo_symbol(sym), rng, interval)


def chart_range(day, today, interval):
    """The smallest Yahoo range that still holds the session of `day`."""
    back = (today - day).days
    if back <= 0:
        return "1d"
    if back <= 6:
        return "5d"
    if interval == "1m":
        raise EntryError("Yahoo keeps 1-minute bars for about a week: use --interval 5m for %s" % day)
    if back <= 28:
        return "1mo"
    if back <= 58:
        return "60d"
    raise EntryError("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)


def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """GET a Yahoo path: query2 first, query1 when query2 answers 429 or 5xx.
    An HTTP 429 gets one retry on the same host after BACKOFF seconds."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown ticker
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise EntryError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise EntryError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise EntryError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def parse_chart(text, sym):
    """Yahoo chart JSON v8 -> {symbol, meta, bars, prev_close, data_time}.
    Bars with a null close are dropped here; off-grid ticks later."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EntryError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise EntryError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": float(c if o is None else o), "h": float(c if h is None else h),
                     "l": float(c if lo is None else lo), "c": float(c), "v": int(at("volume", i) or 0)})
    return {"symbol": str(meta.get("symbol") or sym).upper(), "meta": meta, "bars": bars,
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "data_time": meta.get("regularMarketTime")}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, sym):
    if os.path.isdir(path):
        names = []
        for stem in (sym, yahoo_symbol(sym), instrument(sym)["symbol"]):
            for name in (stem + ".json", stem + ".json.gz"):
                if name not in names:
                    names.append(name)
        for name in names:
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise EntryError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def get_chart(sym, interval, day=None, offline=None, today=None):
    """Bars for one ticker, from Yahoo (range=1d for today) or a saved file."""
    sym = sym.upper()
    if offline:
        f = offline_file(offline, sym)
        try:
            text = read_text(f)
        except (OSError, ValueError) as e:
            raise EntryError("could not read %s: %s" % (f, e))
    else:
        today = today or to_eastern(utc_now()).date()
        rng = chart_range(day or today, today, interval)
        if rng == "1d" and instrument(sym)["market"] != "stock":
            rng = "5d"                     # a 24-hour market: Yahoo's "1d" starts at London midnight
        text = yahoo_get(chart_path(sym, rng, interval))
    ch = parse_chart(text, sym)
    if ch["symbol"] != yahoo_symbol(sym):
        raise EntryError("the chart file holds %s, not %s" % (ch["symbol"], sym))
    gran = ch["meta"].get("dataGranularity")
    if gran and gran != interval:
        raise EntryError("%s has %s bars, not %s: use --interval %s" % (sym, gran, interval, gran))
    return ch


# ---------------------------------------------------------------- the rule

def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise EntryError("%s is not valid JSON: %s" % (path, e))
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
        if isinstance(doc.get("markets"), dict):
            cfg["markets"] = doc["markets"]
        cfg["allow_short"] = doc.get("allow_short") is True
    for k in DEFAULTS:
        parse_hhmm(cfg[k], k)
    return cfg


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def early_cutoff(day, t, close_dt):
    """A cutoff on an early-close day moves up with the close: 15:30 (30 minutes
    before a 16:00 close) becomes 12:30 before a 1:00 p.m. close. A cutoff that
    is already before the early close stays."""
    at = datetime.datetime.combine(day, t)
    if at < close_dt:
        return at
    moved = at - (datetime.datetime.combine(day, CLOSE) - close_dt)
    return moved if moved.time() > OPEN else close_dt


def evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None, side="buy", session_name="ny", symbol=None):
    """Pure function: the setup's status at now_utc from one session's bars.
    side "sell" mirrors it: a close BELOW the range low (or the level)."""
    cfg = cfg or dict(DEFAULTS)
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    inst = instrument(symbol or chart["symbol"])
    stock = inst["market"] == "stock"
    long = side != "sell"
    dec = inst["decimals"]

    def rp(x):
        return None if x is None else round(float(x), dec) + 0.0

    def P(x):
        return px(inst, x)

    def beyond(c, lv):
        return c > lv if long else c < lv

    over = "above" if long else "below"
    edge = ("the range high" if long else "the range low") if setup == "orb" else "the level"
    n_or = or_minutes * 60 // step
    local = to_eastern(now_utc)
    day = local.date()
    res = {"symbol": chart["symbol"] if stock else inst["symbol"], "setup": setup,
           "side": "buy" if long else "sell", "date": day.isoformat(),
           "interval": interval, "or_minutes": or_minutes, "now": local.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting",
           "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
           "last": None, "forming": None, "min_volume_x": min_volume_x,
           "prev_close": chart.get("prev_close"),
           "calendar_known": day.year in CALENDAR_YEARS if stock else True, "_bars": []}
    if not stock:
        res.update(market=inst["market"], session=session_name, decimals=dec)
    dt = chart.get("data_time")
    res["data_time"] = to_eastern(utc_of(dt)).strftime("%Y-%m-%d %H:%M:%S") if dt else None
    ok, why = market_day(inst, day)
    if not ok:
        if stock:
            res.update(status="no-data", phase="closed", reason="the NYSE is closed today (%s)" % why,
                       next_open=next_open(day))
        else:
            res.update(status="no-data", phase="closed",
                       reason="the %s market is closed today (%s)" % (
                           "forex" if inst["market"] == "forex" else "metals", why),
                       next_open=next_market_open(inst, day, session_name))
        return res
    open_dt, close_dt = market_session(inst, day, session_name)
    if stock:
        early = close_dt.time() != CLOSE
        before = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_before"], "no_entries_before"))
        cutoff = early_cutoff(day, parse_hhmm(cfg["no_entries_after"], "no_entries_after"), close_dt)
    else:
        early = False
        m = market_cfg(cfg, inst)
        before = (datetime.datetime.combine(day, parse_hhmm(m["no_entries_before"], "no_entries_before"))
                  if m.get("no_entries_before") else open_dt + datetime.timedelta(minutes=or_minutes))
        cutoff = (datetime.datetime.combine(day, parse_hhmm(m["no_entries_after"], "no_entries_after"))
                  if m.get("no_entries_after") else close_dt - datetime.timedelta(minutes=90))
        cutoff = min(cutoff, close_dt)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res.update(opens=hm(open_dt), closes=hm(close_dt),
               early_close=NYSE_EARLY_CLOSE.get(day.isoformat()) if stock else None,
               no_entries_before=hm(before), no_entries_after=hm(cutoff))

    by_slot = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue                       # Yahoo's live tick (10:49:45) is not a bar
        et = to_eastern(utc_of(b["t"]))
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        slot = int((et - open_dt).total_seconds()) // step
        by_slot[slot] = dict(b, et=et, end=et + datetime.timedelta(seconds=step), slot=slot)
    bars, pending = [], None
    now_ts = ts_of(now_utc)
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        end_ts = b["t"] + step
        if end_ts <= now_ts:
            # Closed by the clock. On live data it counts once Yahoo has caught up: its last
            # trade is at or after the close, a later bar exists, or a minute has passed.
            if dt is None or dt >= end_ts or slot < top or now_ts >= end_ts + SETTLE:
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": rp(b["c"])}
                res["lag_note"] = ("Yahoo's last trade is %s, before the %s bar's close: it counts once Yahoo "
                                   "catches up (a minute at most)" % (to_eastern(utc_of(dt)).strftime("%H:%M:%S"),
                                                                       short(b["et"])))
        elif b["et"] <= local:
            res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": rp(b["c"])}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    if local < open_dt:
        tail = " and the %d-minute range is set at %s" % (or_minutes, short(or_end)) if setup == "orb" else ""
        if stock:
            res.update(phase="pre-open", reason="the NYSE opens at 9:30%s" % tail)
        else:
            res.update(phase="pre-open", reason="the %s session opens at %s New York time%s" % (
                SESSIONS[session_name][0], short(open_dt), tail))
        return res

    or_bars = [b for b in bars if b["slot"] < n_or]
    avg_vol = (sum(b["v"] for b in or_bars) / float(len(or_bars))) if or_bars else None
    if or_bars:
        res["opening_range"] = {"high": rp(max(b["h"] for b in or_bars)), "low": rp(min(b["l"] for b in or_bars)),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": local >= or_end, "avg_volume": int(round(avg_vol))}
    if setup == "orb":
        if local < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, short(or_end)))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="Yahoo sent no bars for the opening range")
            return res
        lvl = max(b["h"] for b in or_bars) if long else min(b["l"] for b in or_bars)
    else:
        lvl = float(level)
    res["level"] = rp(lvl)

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in bars:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if beyond(b["c"], lvl):
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": rp(last["c"]),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if local < close_dt else "after-close",
                   reason="the %s bar closed %s %s" % (short(trig["et"]), over, edge))
        res["trigger"] = {"time": ampm(trig["end"]), "close_time": hm(trig["end"]), "bar_start": hm(trig["et"]),
                          "bar_index": trig["slot"], "price": rp(trig["c"]), "close": trig["c"],
                          "volume": trig["v"], "volume_x": r2(vx), "pct_over": r2(100.0 * (trig["c"] / lvl - 1))}
        res["minutes_since"] = int((local - trig["end"]).total_seconds() // 60)
        if dt and utc_of(dt) < eastern_to_utc(trig["end"]):
            res["data_note"] = ("Yahoo's last trade is %s, before this bar closed: its close can still move %s"
                                % (to_eastern(utc_of(dt)).strftime("%H:%M:%S"),
                                   "a cent or two" if stock else "a little"))
        return res
    if not bars:
        res.update(status="no-data", phase="open", reason="Yahoo sent no bars for %s yet" % day.isoformat())
        return res
    what = "close %s %s %s" % (over, edge, P(lvl)) if setup == "orb" else "close %s %s" % (over, P(lvl))
    if local >= cutoff and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-close" if local >= close_dt else "after-cutoff",
                   reason="no %s by %s, the last time for a new entry%s" % (
                       what, short(cutoff), " (1:00 p.m. early close today)" if early else ""))
        return res
    res["phase"] = "open"
    if beyond(last["c"], lvl) and last in thin:
        res["reason"] = "the %s bar closed %s at %s but on %.2fx the range's volume, under your %.2fx" % (
            short(last["et"]), over, P(last["c"]), last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif beyond(last["c"], lvl) and last["end"] < before:
        res["reason"] = "the %s bar closed %s at %s, but no entries before %s (config)" % (
            short(last["et"]), over, P(last["c"]), short(before))
    else:
        gap = lvl - last["c"]
        res["reason"] = "last close %s (%s bar), %s %s %s %s (%s)" % (
            P(last["c"]), short(last["et"]), P(abs(gap)), "under" if gap >= 0 else "over",
            edge, P(lvl), beyond_txt(inst, last["c"], lvl))
    return res


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now_utc):
    """trader/entry.json, the contract shape (plus a plain-words reason)."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    doc = {"created_by": "trader-entry", "symbol": res["symbol"], "setup": res["setup"], "side": res.get("side", "buy"),
           "date": res["date"], "status": res["status"], "time": t.get("time"), "bar_start": t.get("bar_start"),
           "bar_index": t.get("bar_index"), "price": t.get("price"), "level": res.get("level"),
           "interval": res["interval"],
           "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
           "source": "yahoo", "checked_at": iso(now_utc), "reason": res.get("reason")}
    if res.get("market"):
        doc.update(market=res["market"], session=res["session"], decimals=res["decimals"],
                   session_opens=res.get("opens"), session_closes=res.get("closes"))
    return doc


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"pre-open": "NOT OPEN YET", "forming": "RANGE FORMING", "closed": "MARKET CLOSED"}
SHORT = {"pre-open": "not open", "forming": "forming", "closed": "closed"}


def setup_name(res):
    inst = instrument(res["symbol"])
    long = res.get("side", "buy") != "sell"
    if res["setup"] == "orb":
        if inst["market"] == "stock":
            return "opening-range breakout (%d-minute range)" % res["or_minutes"]
        return "%s-session range %s (%d-minute range)" % (
            SESSIONS[res.get("session", "ny")][0], "breakout" if long else "breakdown", res["or_minutes"])
    over = "above" if long else "below"
    return "close %s %s" % (over, px(inst, res["level"])) if res["level"] is not None else "close %s a level" % over


def describe(res):
    """Plain lines for a person."""
    inst = instrument(res["symbol"])
    stock = inst["market"] == "stock"
    long = res.get("side", "buy") != "sell"
    over = "above" if long else "below"
    edge = ("the range high" if long else "the range low") if res["setup"] == "orb" else "the level"

    def P(x):
        return px(inst, x)

    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    head = "%s  %s %s ET  %s on %s bars (Yahoo, free, can lag)" % (
        res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), setup_name(res), bars)
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += ", data to %s" % res["data_time"][11:]
    out = [head]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high %s, low %s (%d of %d bars%s)." % (
            short(start), short(end), P(orr["high"]), P(orr["low"]), orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        out.append("TRIGGERED %s at %s: the %s bar (bar %d) closed %s %s %s (%s%s)." % (
            t["time"], P(t["price"]), short(datetime.datetime.strptime(t["bar_start"], "%H:%M")), t["bar_index"],
            over, edge, P(res["level"]), beyond_txt(inst, t["close"], res["level"]) if not stock else "%+.2f%%" % t["pct_over"],
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("That was %d minutes ago; the last close is %s (%s bar). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], P(res["last"]["close"]), res["last"]["bar_start"]))
        if res.get("data_note"):
            out.append(res["data_note"] + ".")
        if res.get("minutes_since", 0) > step:
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you %s: /trader-exit "
                   "(exit.py plan --from trader/entry.json)." % ("buy" if long else "sell"))
    else:
        out.append("%s  %s." % (label, res["reason"]))
        f = res.get("forming")
        if res.get("lag_note") and res["status"] == "waiting":
            out.append(res["lag_note"] + ".")
        elif f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming (%s so far, closes %s); it does not count until it closes." % (
                f["bar_start"].lstrip("0"), P(f["price"]), f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s New York." % res["next_open"])
    if res.get("early_close"):
        out.append("Early close today at 1:00 p.m. (%s): no new entries after %s." % (
            res["early_close"], res["no_entries_after"]))
    if not res.get("calendar_known"):
        out.append("Holiday list covers %s only: check nyse.com for this date." % " and ".join(map(str, CALENDAR_YEARS)))
    if inst["market"] == "metal":
        out.append("These are COMEX %s futures prices (%s), about 10 minutes delayed. Spot %s at your broker "
                   "trades a few dollars away: check its price before you act." % (
                       inst["name"], inst["yahoo"], inst["name"]))
    elif inst["market"] == "forex":
        out.append("All times are New York time. Yahoo's forex bars are indicative mid prices with no volume; "
                   "your broker's price and spread will differ a little.")
    return out


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    inst = instrument(res["symbol"])
    long = res.get("side", "buy") != "sell"
    over = "above" if long else "below"

    def P(x):
        return px(inst, x)

    start = short(b["et"])
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far %s to %s" % (P(min(x["l"] for x in seen)), P(max(x["h"] for x in seen)))
        if b["slot"] == n_or - 1:
            note = "opening range set: high %s, low %s" % (P(orr["high"]), P(orr["low"]))
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed %s %s (%s). Get in now if you take it: %s, %s" % (
            over, P(lvl), "%+.2f%%" % t["pct_over"] if inst["market"] == "stock" else beyond_txt(inst, t["close"], lvl),
            t["time"], P(t["price"]))
    elif lvl is None:
        note = ""
    elif (b["c"] > lvl) if long else (b["c"] < lvl):
        note = "%s %s" % (over, P(lvl))
        if res["setup"] == "level" or b["slot"] >= n_or:
            avg = orr.get("avg_volume")
            if res.get("min_volume_x") and avg:
                note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
            elif b["end"] < datetime.datetime.combine(b["et"].date(), parse_hhmm(res["no_entries_before"], "x")):
                note += ", but no entries before %s" % res["no_entries_before"]
    else:
        note = "%s %s %s" % (P(abs(lvl - b["c"])), "under" if long else "over", P(lvl))
    return "%5s bar  closed %s  %s" % (start, P(b["c"]), note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now_utc) -> chart, clock() -> naive UTC, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5m")]
    inst = instrument(opts.get("symbol") or "")
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        local = to_eastern(now)
        day = local.date()
        s = market_session(inst, day, opts.get("session_name", "ny"))
        if not s:
            emit("%s: market closed today (%s). Nothing to watch." % (day.isoformat(), market_day(inst, day)[1]))
            return 1, None
        open_dt, close_dt = s
        if local < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                emit("%s ET  %s: watching from the first bar's close at %s." % (
                    local.strftime("%H:%M"), "not open yet" if local < open_dt else "open, no bar closed yet",
                    short(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = eastern_to_utc(open_dt) + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s ET  could not read bars: %s" % (local.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or local >= close_dt:
            emit("EXPIRED  %s." % (res["reason"] or "the market closed without a trigger"))
            return 1, res
        if until and local >= until:
            emit("Stopped at --until %s without a trigger. %s." % (short(until), res["reason"]))
            return 1, res
        # sleep until just after the next bar closes; poll every `every` seconds if Yahoo is behind
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - local).total_seconds()
        cutoff = datetime.datetime.combine(day, parse_hhmm(res.get("no_entries_after") or "15:30", "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > local:
                wait = min(wait, (stop_at - local).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _opts(a, cfg, sym=None):
    inst = instrument(sym or "")
    short_side = a.side in ("sell", "short")
    if short_side and inst["market"] == "stock":
        raise EntryError("v2 plans longs only: no short entries. Watch for a buy (--side buy) instead.")
    if short_side and not cfg.get("allow_short"):
        raise EntryError('short entries are off. To watch %s for a sell, set "allow_short": true in '
                         "trader/config.json (a short loses money when the price rises)" % inst["symbol"])
    if inst["market"] == "forex" and a.min_volume_x is not None:
        raise EntryError("--min-volume-x does not work for forex: Yahoo has no volume for currency pairs")
    if a.session and a.session not in SESSIONS:
        raise EntryError("--session is %s" % " or ".join(sorted(SESSIONS)))
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    opts = {"setup": a.setup, "or_minutes": a.or_minutes or default_or_minutes(inst), "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg}
    if inst["market"] != "stock":
        opts.update(side="sell" if short_side else "buy", symbol=inst["symbol"],
                    session_name=a.session or market_cfg(cfg, inst).get("session") or "ny")
        if opts["session_name"] not in SESSIONS:
            raise EntryError('"session" in the config is %s' % " or ".join(sorted(SESSIONS)))
    return opts


def _load(a, sym, now):
    """(chart, now). An offline file without --now is read as of its own time."""
    real_today = to_eastern(utc_now()).date()
    if a.offline and now is None:
        ch = get_chart(sym, a.interval, offline=a.offline)
        return ch, (utc_of(ch["data_time"]) if ch.get("data_time") else utc_now())
    now = now or utc_now()
    return get_chart(sym, a.interval, to_eastern(now).date(), a.offline, real_today), now


def cmd_check(a):
    cfg = read_config(a.config)
    sym = a.symbol.upper()
    opts = _opts(a, cfg, sym)
    chart, now = _load(a, sym, parse_now(a.now) if a.now else None)
    res = evaluate(chart, now, **opts)
    if a.write:
        write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    sym = a.symbol.upper()
    opts = _opts(a, cfg, sym)
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    start = parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, None)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, _sleep
    real_today = to_eastern(utc_now()).date()

    def provider(now):
        return get_chart(sym, a.interval, to_eastern(now).date(), a.offline, real_today)

    until = None
    if a.until:
        t = parse_hhmm(a.until, "--until")
        until = datetime.datetime.combine(to_eastern(now_fn()).date(), t)

    def on_trigger(res, now):
        write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until,
                         on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    if not os.path.exists(a.watchlist):
        raise EntryError("no watchlist at %s: run /trader-scanner first (it writes the hits there)" % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (a.watchlist, e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % a.watchlist)
    if a.offline and not os.path.isdir(a.offline):
        raise EntryError("watchlist --offline wants a folder of <TICKER>.json files")
    now = parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        sym = str(h["symbol"]).upper()
        try:
            o = _opts(a, cfg, sym)
        except EntryError as e:
            rows.append((sym, None, str(e)))
            continue
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        try:
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i and not a.offline:
                _sleep(PAUSE)
            chart, when = _load(a, sym, now)
            rows.append((sym, evaluate(chart, when, **o), None))
        except EntryError as e:
            rows.append((sym, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows], indent=2))
    else:
        print("%-6s  %-9s  %-8s  %-8s  %-9s  %-9s  %s" % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST", "VS LEVEL"))
        for s, r, e in rows:
            if e:
                print("%-6s  error: %s" % (s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
            print("%-6s  %-9s  %-8s  %-8s  %-9s  %-9s  %s" % (
                s, status[:9], t.get("time") or "-", px(instrument(s), t["price"]) if t else "-",
                px(instrument(s), r["level"]) if r.get("level") is not None else "-",
                px(instrument(s), last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <TICKER> --write, and /trader-exit." % (n, len(rows)))
        print("Free Yahoo bars, unofficial and can lag. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry Timer (trader-entry): watches a setup bar by bar.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int,
                       help="opening range length (default 15; 30 for forex and metals)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"],
                       help="stocks: buy only. Forex and metals: sell needs \"allow_short\": true")
        p.add_argument("--session", help="forex and metals: ny (8:00-17:00 New York, default) or london")
        p.add_argument("--now", help='pretend it is this New York time, e.g. "2026-10-19 10:15"')
        p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON (.json/.json.gz) or a folder")
        p.add_argument("--config", default="trader/config.json")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while Yahoo is behind")
    p.add_argument("--until", help="stop at this New York time (HH:MM)")
    p.add_argument("--out", default="trader/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except EntryError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
