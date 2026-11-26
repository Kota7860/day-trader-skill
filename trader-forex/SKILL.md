---
name: trader-forex
description: >-
  Forex Market. The currency market's own scanner: knows the 21 major and
  cross currency pairs plus gold and silver (EURUSD, GBPJPY, XAUUSD ...),
  says which of the four trading sessions (Sydney, Tokyo, London, New York)
  are open right now and when the next one opens, and scans every pair at
  once for session-range breakouts: a 5-minute bar that CLOSED above the
  first 30 minutes' high (a buy) or below its low (a sell). Writes the hits
  to trader/watchlist.json for /trader-entry, the way /trader-scanner does
  for US stocks. Free Yahoo data, all times in New York time. It never
  places an order. Use when the user says "scan forex", "scan the currency
  market", "which pairs are breaking out", "forex setups", "what pairs
  should I watch", "is the forex market open", "which sessions are open",
  "when does London open", "forex market hours", "gold breakout", or at the
  start of a forex trading day before /trader-entry.
argument-hint: "[pairs | sessions | scan] [--session ny|london]"
---

# trader-forex

The Forex Market skill does for currencies what the Chart Scanner does for US
stocks. The stock scanner starts from Nasdaq Trader's list of US stocks; this
one starts from a fixed list of currency pairs and two metals. It answers
three questions: what can I trade, is the market open, and which pairs are
breaking out. A scan is a list of charts that matched a rule, not a list of
trades to take. Most people who trade forex with leverage lose money.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
It needs the trader-entry skill installed next to it: both use the same
breakout rule.

```bash
python3 "${CLAUDE_SKILL_DIR}/forex.py" pairs                         # the 23 pairs and metals
python3 "${CLAUDE_SKILL_DIR}/forex.py" sessions                      # what is open now, what opens next
python3 "${CLAUDE_SKILL_DIR}/forex.py" scan                          # New York session, every pair
python3 "${CLAUDE_SKILL_DIR}/forex.py" scan --session london         # London session
python3 "${CLAUDE_SKILL_DIR}/forex.py" scan --group majors           # majors | crosses | metals | all
python3 "${CLAUDE_SKILL_DIR}/forex.py" scan --symbols EURUSD,GBPJPY,XAUUSD
python3 "${CLAUDE_SKILL_DIR}/forex.py" scan --now "2026-11-23 10:00" # replay a past moment (New York time)
```

## The universe

| group | pairs |
| --- | --- |
| majors | EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD |
| crosses | EURGBP, EURJPY, EURCHF, EURAUD, EURCAD, GBPJPY, GBPCHF, GBPAUD, AUDJPY, AUDNZD, CADJPY, CHFJPY, NZDJPY, AUDCAD |
| metals | XAUUSD (gold), XAGUSD (silver) |

A pair can be written any usual way: `EURUSD`, `EUR/USD`, `eur_usd`. Most pairs
quote to 5 decimals and a pip is 0.0001; yen pairs quote to 3 and a pip is
0.01. Gold and silver are COMEX futures prices (`GC=F`, `SI=F`), about 10
minutes delayed, because there is no free spot feed. Say so whenever you quote
a metals price.

## Sessions

The forex week runs from Sunday 17:00 to Friday 17:00 New York time, and is
closed on December 25 and January 1. Inside it, four city sessions overlap:

| session | its own hours | New York time in November |
| --- | --- | --- |
| Sydney | 7:00 to 16:00 Sydney | 15:00 to 0:00 |
| Tokyo | 9:00 to 18:00 Tokyo | 19:00 to 4:00 |
| London | 8:00 to 16:30 London | 3:00 to 11:30 |
| New York | 8:00 to 17:00 New York | 8:00 to 17:00 |

`sessions` works out daylight time for each city itself, so the New York times
move through the year. When two are open together (London and New York, 8:00
to 11:30 New York in winter) spreads are usually tightest and moves biggest.
Exit code 0 when the forex market is open, 1 when it is closed.

The scan and the rest of the pack trade the **London** or **New York**
session. Sydney and Tokyo are shown for context only.

## The scan

For each pair it reads Yahoo's free 5-minute bars, builds the session's
opening range (first 30 minutes by default: 8:00 to 8:30 for New York, the
first half hour of London) and checks both ways:

- **buy**: the first completed bar after the range that CLOSED above the range high
- **sell**: the first completed bar that CLOSED below the range low

A wick through the level does not count. A pair that did both shows the earlier
one. It also lists the pairs closest to breaking out, in pips (percent for
metals). It is the same rule as trader-entry, so a hit here is a trigger there.

Hits go to `trader/watchlist.json` (only when there is at least one):

```json
{"created_by": "trader-forex", "mode": "session-orb", "session": "ny",
 "hits": [{"symbol": "EURAUD", "market": "forex", "side": "buy", "session": "ny",
           "time": "8:50 AM", "price": 1.65502, "level": 1.65461}]}
```

Reading a scan for the user:

1. Lead with the count and the session: "At 10:00 New York, 23 of 23 pairs
   had broken out of the New York session's first 30 minutes."
2. Give the hits as a short table: pair, buy or sell, time, price, how far
   beyond the level in pips.
3. Point out that many pairs moving the same way at once usually means one
   currency is moving (for example every yen pair selling off is the yen
   strengthening), not many separate setups.
4. A breakout of 1 or 2 pips is small; spreads on crosses are often 2 or 3
   pips. Say so rather than letting a tiny breakout look strong.
5. **Sell hits are information.** Trading one needs `"allow_short": true` in
   `trader/config.json`, set by the user. Never turn it on yourself.

Then hand off: the user picks one pair, and `/trader-entry` checks it
(`entry.py check EURAUD --session ny`), then `/trader-exit`, `/trader-budget`,
`/trader-risk` and `/trader-broker` as usual. Every gate still applies.

## What it does not do

- No economic calendar. Rate decisions, inflation and jobs reports move
  currencies more than anything else, and this pack has no free source for
  them. Tell the user to check one before trading a session with a big
  release, especially around 8:30 New York.
- No daily or trend scan yet, only the session breakout.
- No crypto.
- The Time Machine cannot replay a forex pair.

## Data

Yahoo's chart endpoint is free and unofficial: it can lag, change or limit
you. Forex bars there are indicative mid prices with no volume; your broker's
bid, ask and spread will differ a little. A full scan is 23 requests, about
10 seconds with the polite pause between them. Check anything that matters
against your broker before you act.

## Rules

- Education and tooling, not financial advice. A scan is a list of charts that matched a rule, not a recommendation.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
