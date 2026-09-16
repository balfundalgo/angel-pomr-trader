# POMR — The First Fifteen Minutes

Pre-Open Momentum Reclaim on NSE F&O equities, on the Angel One SmartAPI.
Implements Balfund Strategy Note 01 v1.3.

A stock gaps. It tests its opening price from the wrong side. Then it takes
it back. We trade that last move — long on the gainers, short on the losers.
Flat by 09:30 every day.

---

## The morning

| Time | What happens |
|---|---|
| 09:08:30 | Rank every F&O stock on the pre-open auction price against yesterday's close. Lock the ten names. |
| 09:14:00 | Live feed connected and verified. |
| 09:15:00 | Market opens. Each stock's opening price is recorded — this is the line. |
| +0 / 15 / 30s | The early check. Anything already out and back is taken immediately. Configurable; 0 removes the warm-up entirely. |
| 09:28:00 | Last entry. After this, setups are abandoned. |
| 09:30:00 | Everything closed. Longs sold, shorts covered. |

---

## Running it

```bash
pip install -r requirements.txt
python src/main.py
```

Enter your Angel One client ID, API key, MPIN and TOTP secret in the panel
and press **Save settings**. They are written to `settings.json` beside the
executable and never leave the machine.

### Run modes

| Mode | What it does |
|---|---|
| `SCAN_ONLY` | Ranks the F&O list, writes the day's ten names to CSV, stops. No feed, no orders. |
| `PAPER` | Full live decisions on the live feed, simulated fills. |
| `LIVE` | Real orders. |

**Start in `SCAN_ONLY`.** Stage 1 of the build plan is ten sessions of
scanner output checked by hand against the exchange. It looks like the least
interesting part and it is the most important — if the pre-open data is
being read wrong, every test and every trade after it is wrong too, and you
would not know.

---

## Output

Four files per session, in `logs/`:

| File | Contents |
|---|---|
| `pomr_scan_YYYYMMDD.csv` | The day's ten names with auction price, previous close, gap %, auction value and source. |
| `pomr_rejects_YYYYMMDD.csv` | Everything the filters removed, with the reason. Read this alongside the scan. |
| `pomr_trades_YYYYMMDD.csv` | One row per completed round trip: the line, the test extreme and its depth, entry, stop, size, risk, exit, reason, P&L, hold time. |
| `pomr_orders_YYYYMMDD.csv` | Every order event — submission, fill, rejection, resting stop, cancellation. |
| `pomr_activity_YYYYMMDD.log` | The full narrative of the morning. |

---

## The rules, as implemented

**Choosing the ten.** Pre-open auction price against yesterday's close,
ranked across the F&O stock list. Removed before ranking, never after: names
on the manual exclusion list (ex-dividend, splits), stocks with no auction
trades, thin auctions, moves under 0.75%, and stocks under ₹50. If fewer
than five survive on a side, the shorter list is traded. The sixth-biggest
mover is never promoted to make a round number.

**The line.** The opening price at 09:15, read from the exchange's own
opening print on the quote feed (`open_price_of_the_day`) rather than
inferred from the first tick. It is cross-checked against the auction price
and a disagreement over 0.5% is logged as a warning.

**Clearly through.** `max(0.05% of the opening price, 2 ticks)`. Applied to
both the test and the entry crossing. On the note's SBIN example this is
what produces the ₹839.20 fill at the check rather than the ₹838.30 print
eight seconds earlier — the marginal recovery is not yet the reclaim.

**The early check.** Not a separate code path. Ticks feed the same state
machine from 09:15:00, but no order may be placed before the check moment.
A stock that completed its out-and-back inside the first thirty seconds is
therefore standing at an entry condition the instant the check arrives.

The delay is a free numeric field, not a two-value dropdown. Setting it to
0 removes the warm-up: entries fire from 09:15:00 onward, the moment a
test-and-reclaim completes. That is a real change in behaviour rather than
a faster version of the same thing — see the note on it below.

**The stop.** Just the other side of the extreme reached during the test —
one tick under the low for a long, one tick above the high for a short.
Widened to a 0.25% floor if the test was very shallow. The trade is skipped
outright if the test ran deeper than 1.5% through the line.

**Size.** Not chosen in advance; it falls out of where the stop sits. One
percent of the account at risk, recalculated each morning from the live RMS
balance. No position larger than a third of the account.

**Exits.** Two, and no others: the stop is hit, or the clock reaches 09:30.
No profit target, no partial exits.

**The trail (optional, off by default).** A three-stage ratchet, all three
values expressed as a percentage of the entry price so the behaviour is
identical on a Rs 670 stock and a Rs 9,200 one:

1. Profit reaches `trail_trigger_pct` -> the stop moves to the entry price.
2. Every further `trail_step_pct` of profit -> the stop moves another
   `trail_move_pct` in your favour, counted from the entry price.
3. The stop only ever tightens. A retrace never gives back a step.

Formally, once profit >= X: `steps = floor((profit - X) / Y)` and
`stop = entry +/- steps * Z`. The result is held at least `min_stop_pct`
behind the current price, so a Z larger than Y can never walk the stop
through the market. The resting broker order is amended with `modifyOrder`
rather than cancelled and replaced, because cancelling leaves the position
naked for the round trip. An exit on a moved stop is logged as `TRAIL_HIT`
rather than `STOP_HIT`, and the trade CSV gains `initial_stop`,
`final_stop`, `trail_steps`, `best_price` and `best_excursion` so the trail
can actually be measured afterwards.

Option positions have no resting stop at the broker, so the trail there is
engine-side only.

---

## Things you should decide before going live

**The position cap is the binding constraint on every trade, not the risk
budget.** With 1% risk and a 33.33% position cap, the cap bites whenever the
stop is nearer than 3% of price. The test-depth guard rail already caps the
stop at 1.5%, so that is every trade this strategy will ever take. Effective
risk per position lands somewhere around 0.2–0.5%, not 1%. The note's own
Tata Motors example shows exactly this (₹1,890 risked against a ₹10,000
budget). This is faithful to the note, but it means the "ten percent of the
account at risk in a fifteen-minute window" warning on page 8 overstates the
real exposure by roughly five times. Worth resolving before Stage 3, because
it changes what the settings review is actually measuring.

**A zero-second check is not just a faster check.** With no warm-up, a
test-and-reclaim can complete on three prints inside two seconds — a spread
bounce on a gapping stock, not a move. The note allows for this at 15
seconds ("will occasionally buy a stock that was only briefly under its
opening price"); at 0 it stops being occasional. The consequence is not the
individual trade but the daily cap: the first four signals take all four
slots, and at 0 those four will systematically be the noisiest names rather
than the cleanest, leaving nothing for a genuine setup at 09:18. If the
setting is going to 0, raise `cross_buffer_pct` from 0.05 to roughly
0.15-0.20 so "clearly through" still means something in the first seconds,
and prove it in PAPER before LIVE. A warning is logged at startup whenever
the value is 0.

**Corporate actions are a manual input.** Angel exposes no ex-dividend or
split calendar. Names to remove go in the exclusions box each morning. A
stock going ex-dividend opens lower for accounting reasons, looks like a top
loser, and nothing has happened — so this field is not optional on a
dividend-heavy morning.

**Pre-open dissemination is not guaranteed.** Angel publishes the auction
equilibrium price on the ordinary quote path, which is what the scanner
reads. Where Angel returns nothing usable, the NSE public pre-open board is
used as a fallback and the source is recorded per name in the scan CSV.
Watch the `source` column during Stage 1 — if it is frequently `NSE`, the
Angel path is not reliable at 09:08 on your connection and that needs
solving before anything else.

**The option route cannot have a resting stop.** The signal and the stop are
both stock price levels, so an option position is covered by the engine
only, not by an order sitting with the broker. If the connection drops with
an option leg open, nothing protects it. Build and prove the stock version
first, exactly as the note recommends.

**Short selling.** Intraday cash shorts are squared off the same day, which
ours always are. Some brokers still block individual scrips at short notice;
a rejection is detected and reported rather than assumed away. Set
`Short side via` to `FUTURES` to route shorts through the near-month stock
future instead — note that lot sizing there cannot be fine-tuned, so a name
whose single lot risks more than the budget falls back to the cash short.

---

## Tests

```bash
python src/test_logic.py
```

52 offline assertions covering the buffer, both directions of the state
machine, the early check, the guard rails, the filters, ranking with a short
list, stop-breach direction and sizing. All five worked examples from pages
6–7 of the note are replayed tick by tick. No broker, no network. The
GitHub Actions workflow runs these before it builds the EXE.

---

## Build

Push to `main`; the Actions tab produces `POMR_Trader.exe` as a downloadable
artifact.

---

Balfund Trading Pvt Ltd · balfund.com
