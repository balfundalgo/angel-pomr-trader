"""
test_logic.py
=============
Offline assertions for every rule in the strategy note. No broker, no
network, no GUI — run this after any change to strategy.py or scanner.py.

    python src/test_logic.py

The five worked examples from the note (pages 6-7) are replayed tick by tick
and checked against the outcomes printed there.
"""

from datetime import datetime, timedelta

import config
import strategy as S
import scanner

PASS = 0
FAIL = 0


def check(label, got, want=True):
    global PASS, FAIL
    ok = (got == want)
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print(f"   FAIL  {label}\n         got {got!r}, wanted {want!r}")
    return ok


def approx(label, got, want, tol=0.011):
    global PASS, FAIL
    if abs(float(got) - float(want)) <= tol:
        PASS += 1
    else:
        FAIL += 1
        print(f"   FAIL  {label}\n         got {got!r}, wanted ~{want!r}")


def mk(name, side, auction, prev, tick=0.05):
    return S.StockState(name=name, symbol=f"{name}-EQ", token="1",
                        side=side, rank=1, tick_size=tick, lot_size=0,
                        auction_price=auction, prev_close=prev,
                        gap_pct=round((auction - prev) / prev * 100, 3))


def replay(st, prices, t0=None):
    """Feed a price series, returning the tick index where ENTER first fired."""
    t0 = t0 or config.open_dt()
    fired = None
    for i, p in enumerate(prices):
        sig = st.on_tick(p, t0 + timedelta(seconds=i))
        if sig == "ENTER" and fired is None:
            fired = i
    return fired


# ======================================================================
def t_buffer():
    print("\n[1] 'Clearly through' and not simply 'through'")
    config.STRATEGY["cross_buffer_pct"] = 0.05
    config.STRATEGY["min_cross_ticks"] = 2

    st = mk("TATAMOTORS", "GAINER", 748.0, 719.9)
    st.set_open_price(748.0, "test")
    # 0.05% of 748 = 0.374, two ticks = 0.10 -> percentage wins
    approx("buffer is the larger of pct and ticks", st.buffer, 0.374)

    # a cheap stock where the tick floor should win instead
    st2 = mk("PENNY", "GAINER", 60.0, 58.0)
    st2.set_open_price(60.0, "test")
    approx("tick floor applies on low-priced stocks", st2.buffer, 0.10)

    # one paisa the wrong side must NOT arm the stock
    st3 = mk("X", "GAINER", 748.0, 719.9)
    st3.set_open_price(748.0, "test")
    st3.on_tick(747.99, config.open_dt())
    check("a one-paisa dip does not count as a test",
          st3.state, S.WAITING_TEST)


def t_gainer_path():
    print("\n[2] The slower path — gainer tests below, then reclaims")
    st = mk("TATAMOTORS", "GAINER", 748.0, 719.9)
    st.set_open_price(748.0, "test")

    fired = replay(st, [752.0, 750.1, 748.2, 746.0, 744.20, 745.5,
                        747.9, 748.4, 749.0])
    check("entry fired", fired is not None)
    check("state is TESTED at the entry moment", st.state, S.TESTED)
    approx("the low out there became the reference", st.test_extreme, 744.20)
    approx("stop sits one tick under that low", st.build_stop(), 744.15)


def t_loser_mirror():
    print("\n[3] The mirror — loser rallies above, then fails")
    st = mk("INDUSINDBK", "LOSER", 941.0, 980.2)
    st.set_open_price(941.0, "test")

    fired = replay(st, [938.2, 940.0, 941.9, 944.0, 946.80, 945.0,
                        942.0, 940.40, 939.0])
    check("entry fired", fired is not None)
    approx("the high out there became the reference", st.test_extreme, 946.80)
    approx("stop sits one tick above that high", st.build_stop(), 946.85)


def t_never_tested():
    print("\n[4] Never tested the line — no trade")
    st = mk("RELIANCE", "GAINER", 1486.0, 1445.5)
    st.set_open_price(1486.0, "test")
    fired = replay(st, [1491.2, 1495.0, 1489.0, 1487.5, 1502.0, 1509.40])
    check("no entry", fired, None)
    check("still waiting for the test", st.state, S.WAITING_TEST)
    check("no stop reference exists", st.test_extreme, None)


def t_early_check():
    print("\n[5] The early check — out and back inside the first 30 seconds")
    st = mk("SBIN", "GAINER", 838.0, 812.0)
    st.set_open_price(838.0, "test")

    t0 = config.open_dt()
    st.on_tick(834.60, t0 + timedelta(seconds=8))            # below the line
    sig22 = st.on_tick(838.30, t0 + timedelta(seconds=22))   # back above it
    # 838.30 is above the line but not CLEARLY above it — the buffer on an
    # 838 stock is 0.42, so the signal is not yet armed. By the check moment
    # the stock is at 839.20, which is, and that is the price the note
    # records as the fill.
    check("a marginal recovery is not yet the reclaim", sig22, None)
    sig30 = st.on_tick(839.20, t0 + timedelta(seconds=30))
    check("the pattern is complete at the check", sig30, "ENTER")
    check("it is standing at TESTED when the check arrives", st.state, S.TESTED)
    approx("stop from the 09:15:08 low", st.build_stop(), 834.55)

    # ...and if it slips back under before the check, it is not an entry
    st2 = mk("SBIN", "GAINER", 838.0, 812.0)
    st2.set_open_price(838.0, "test")
    st2.on_tick(834.60, t0 + timedelta(seconds=8))
    st2.on_tick(838.30, t0 + timedelta(seconds=22))
    sig2 = st2.on_tick(835.00, t0 + timedelta(seconds=28))
    check("slipping back under is not an entry", sig2, None)
    check("and the extreme keeps updating", st2.test_extreme <= 834.60)


def t_deep_test_disqualified():
    print("\n[6] Guard rail — a test deeper than the cap kills the setup")
    config.STRATEGY["max_test_pct"] = 1.5
    st = mk("HINDALCO", "GAINER", 612.0, 594.0)
    st.set_open_price(612.0, "test")
    replay(st, [610.0, 605.0, 600.0])          # 1.96% through the line
    check("disqualified", st.state, S.DISQUALIFIED)
    fired = replay(st, [613.0, 615.0])
    check("and it cannot enter afterwards", fired, None)


def t_sizing():
    print("\n[7] Size falls out of the stop, and the guard rails bite")
    s = config.STRATEGY
    s.update({"risk_pct": 1.0, "min_stop_pct": 0.25, "max_position_pct": 33.33})
    cap = 1000000.0

    st = mk("TATAMOTORS", "GAINER", 748.0, 719.9)
    st.set_open_price(748.0, "test")
    st.test_extreme = 744.20

    qty, stop, risk, note = st.size(748.40, 744.15, cap)
    # 10,000 / 4.25 = 2352 shares, but a third of the account at 748.40
    # is only 445 shares
    check("size capped at a third of the account", qty, 445)
    approx("so the trade actually risks well under the budget", risk, 1891.25, 1.0)
    check("and the cap is reported", "capped" in note)

    # a very shallow test would otherwise produce an enormous share count
    st2 = mk("Y", "GAINER", 1000.0, 970.0)
    st2.set_open_price(1000.0, "test")
    qty2, stop2, risk2, note2 = st2.size(1000.10, 999.95, cap)
    approx("stop widened to the 0.25% floor", stop2, 997.60)
    check("widening is reported", "widened" in note2)
    check("share count is now sane", qty2 <= 333)

    # short side sizes identically, and the cap binds there too
    st3 = mk("Z", "LOSER", 500.0, 520.0)
    st3.set_open_price(500.0, "test")
    qty3, stop3, risk3, note3 = st3.size(499.0, 504.0, cap)
    check("short qty is the capped figure, not the budget figure", qty3, 667)
    check("short risk lands under the budget", risk3 < 10000.0)
    check("and the cap is reported on the short side too", "capped" in note3)

    # The cap is the binding constraint whenever the stop is nearer than
    # max_position_pct / risk_pct = 33.33x the risk, i.e. under ~3% of price.
    # Since the test-depth guard rail caps the stop at 1.5%, that is every
    # trade this strategy will ever take. Effective risk is therefore well
    # under 1% — exactly as the worked example on page 6 shows.
    st4 = mk("W", "GAINER", 300.0, 288.0)
    st4.set_open_price(300.0, "test")
    _, _, risk4, note4 = st4.size(300.5, 296.0, cap)   # 1.5% stop
    check("even the widest allowed stop is still capped", "capped" in note4)
    check("so risk stays below the stated budget", risk4 < 10000.0)


def t_filters():
    print("\n[8] What we throw out before ranking")
    s = config.STRATEGY
    s.update({"min_gap_pct": 0.75, "min_price": 50.0,
              "min_auction_value": 500000.0, "enforce_auction_value": True,
              "exclusions": "VEDL", "top_n": 5})

    base = dict(name="", symbol="", token="1", tick_size=0.05, lot_size=0,
                auction_price=100.0, prev_close=95.0, gap_pct=5.26,
                auction_qty=100000, auction_value=10000000.0, source="ANGEL")

    rows = [
        {**base, "name": "GOOD1", "gap_pct": 4.0},
        {**base, "name": "GOOD2", "gap_pct": -3.5},
        {**base, "name": "VEDL", "gap_pct": 6.0},
        {**base, "name": "NOAUC", "auction_qty": 0},
        {**base, "name": "THIN", "auction_value": 10000.0},
        {**base, "name": "CHEAP", "auction_price": 42.0},
        {**base, "name": "NOISE", "gap_pct": 0.2},
    ]
    keep, drop = scanner.apply_filters(rows)
    names = {r["name"] for r in keep}
    check("only the two genuine movers survive", names, {"GOOD1", "GOOD2"})
    reasons = {r["name"]: r["reject_reason"] for r in drop}
    check("manual exclusion", reasons["VEDL"], scanner.R_EXCLUDED)
    check("no auction trades", reasons["NOAUC"], scanner.R_NO_AUCTION)
    check("thin auction", reasons["THIN"], scanner.R_THIN)
    check("price floor", reasons["CHEAP"], scanner.R_PRICE)
    check("move too small", reasons["NOISE"], scanner.R_SMALL)


def t_short_list():
    print("\n[9] A short list is traded short — never topped up")
    config.STRATEGY["top_n"] = 5
    base = dict(symbol="", token="1", tick_size=0.05, lot_size=0,
                auction_price=100.0, prev_close=95.0, auction_qty=1,
                auction_value=1e7, source="ANGEL")
    survivors = [{**base, "name": f"G{i}", "gap_pct": 5.0 - i} for i in range(2)]
    survivors += [{**base, "name": f"L{i}", "gap_pct": -5.0 - i} for i in range(7)]
    out = scanner.rank(survivors)
    g = [r for r in out if r["side"] == "GAINER"]
    l = [r for r in out if r["side"] == "LOSER"]
    check("two gainers stay two", len(g), 2)
    check("seven losers are cut to five", len(l), 5)
    check("the biggest loser ranks first", l[0]["name"], "L6")
    check("and the five smallest losers are dropped",
          {r["name"] for r in l}, {"L6", "L5", "L4", "L3", "L2"})


def t_stop_breach():
    print("\n[10] Stop breach reads the right way on each side")
    st = mk("A", "GAINER", 100.0, 96.0)
    st.set_open_price(100.0, "test")
    st.state = S.ENTERED
    st.qty, st.entry_price, st.stop_price = 10, 100.5, 99.0
    check("long breaches on the way down", st.stop_breached(98.9), True)
    check("long unharmed above it", st.stop_breached(99.5), False)
    st.ltp = 101.0
    approx("long unrealized", st.unrealized(), 5.0)

    sh = mk("B", "LOSER", 100.0, 104.0)
    sh.set_open_price(100.0, "test")
    sh.state = S.ENTERED
    sh.qty, sh.entry_price, sh.stop_price = 10, 99.5, 101.0
    check("short breaches on the way up", sh.stop_breached(101.1), True)
    check("short unharmed below it", sh.stop_breached(100.5), False)
    sh.ltp = 98.5
    approx("short unrealized", sh.unrealized(), 10.0)


def t_one_entry_per_stock():
    print("\n[11] One entry per stock per day")
    st = mk("C", "GAINER", 200.0, 192.0)
    st.set_open_price(200.0, "test")
    replay(st, [199.0, 198.0, 200.5])
    st.state = S.ENTERED            # engine took it
    fired = replay(st, [198.0, 197.0, 201.0])
    check("a second setup on the same name is ignored", fired, None)


def t_trail_off_by_default():
    print("\n[13] The trail is off unless it is switched on")
    config.STRATEGY["trail_enabled"] = False
    st = mk("A", "GAINER", 100.0, 96.0)
    st.set_open_price(100.0, "test")
    st.state = S.ENTERED
    st.entry_price, st.stop_price, st.initial_stop, st.qty = 100.0, 99.0, 99.0, 10
    check("no move even on a large profit", st.trail_target(120.0), None)


def t_trail_long():
    print("\n[14] The ratchet on a long — X then Y/Z, from the entry price")
    s = config.STRATEGY
    s.update({"trail_enabled": True, "trail_trigger_pct": 10.0,
              "trail_step_pct": 5.0, "trail_move_pct": 3.0,
              "min_stop_pct": 0.25})
    st = mk("A", "GAINER", 100.0, 96.0)
    st.set_open_price(100.0, "test")
    st.state = S.ENTERED
    st.entry_price, st.stop_price, st.initial_stop, st.qty = 100.0, 99.0, 99.0, 10

    check("below the trigger nothing moves", st.trail_target(109.0), None)

    be = st.trail_target(110.0)
    approx("at +X the stop goes to cost", be, 100.0)
    st.apply_trail(be, 110.0)
    check("breakeven is recorded", st.breakeven_done, True)
    check("no step taken yet", st.trail_steps, 0)

    a = st.trail_target(115.0)
    approx("one further Y moves the stop one Z", a, 103.0)
    st.apply_trail(a, 115.0)
    b = st.trail_target(120.0)
    approx("two Y moves it two Z", b, 106.0)
    st.apply_trail(b, 120.0)
    check("two steps recorded", st.trail_steps, 2)

    check("a retrace never gives the step back", st.trail_target(117.0), None)
    check("and the stop is still where it was", st.stop_price, 106.0)
    check("the trade counts as trailed", st.trailed(), True)


def t_trail_short_mirror():
    print("\n[15] The ratchet on a short is the same shape, flipped")
    s = config.STRATEGY
    s.update({"trail_enabled": True, "trail_trigger_pct": 10.0,
              "trail_step_pct": 5.0, "trail_move_pct": 3.0})
    st = mk("B", "LOSER", 100.0, 104.0)
    st.set_open_price(100.0, "test")
    st.state = S.ENTERED
    st.entry_price, st.stop_price, st.initial_stop, st.qty = 100.0, 101.0, 101.0, 10

    check("below the trigger nothing moves", st.trail_target(91.0), None)
    be = st.trail_target(90.0)
    approx("at +X the stop goes to cost", be, 100.0)
    st.apply_trail(be, 90.0)
    a = st.trail_target(85.0)
    approx("one step moves the stop DOWN, not up", a, 97.0)
    st.apply_trail(a, 85.0)
    approx("two steps", st.trail_target(80.0), 94.0)
    check("a rally never loosens it", st.trail_target(88.0), None)


def t_trail_clamp():
    print("\n[16] A step bigger than the profit cannot walk the stop into price")
    s = config.STRATEGY
    # Z larger than Y: without the clamp the stop overtakes the market
    s.update({"trail_enabled": True, "trail_trigger_pct": 10.0,
              "trail_step_pct": 5.0, "trail_move_pct": 8.0,
              "min_stop_pct": 0.25})
    st = mk("C", "GAINER", 100.0, 96.0)
    st.set_open_price(100.0, "test")
    st.state = S.ENTERED
    st.entry_price, st.stop_price, st.initial_stop, st.qty = 100.0, 99.0, 99.0, 10

    st.apply_trail(st.trail_target(110.0), 110.0)
    for px in (115.0, 120.0, 125.0, 130.0):
        t = st.trail_target(px)
        if t is not None:
            st.apply_trail(t, px)
        check(f"stop stays behind the market at {px:.0f}", st.stop_price < px)
    approx("and sits a min-stop gap behind", st.stop_price, 130.0 - 0.25, 0.06)


def t_trail_never_widens():
    print("\n[17] The trail only ever tightens")
    s = config.STRATEGY
    s.update({"trail_enabled": True, "trail_trigger_pct": 10.0,
              "trail_step_pct": 5.0, "trail_move_pct": 3.0})
    st = mk("D", "GAINER", 100.0, 96.0)
    st.set_open_price(100.0, "test")
    st.state = S.ENTERED
    # an unusually tight initial stop, already better than breakeven would be
    st.entry_price, st.stop_price, st.initial_stop, st.qty = 100.0, 100.5, 100.5, 10
    check("breakeven does not loosen a better stop", st.trail_target(110.0), None)
    approx("stop untouched", st.stop_price, 100.5)


def t_trail_exit_reason():
    print("\n[18] A trail hit is logged distinctly from the original stop")
    s = config.STRATEGY
    s.update({"trail_enabled": True, "trail_trigger_pct": 10.0,
              "trail_step_pct": 5.0, "trail_move_pct": 3.0})
    st = mk("E", "GAINER", 100.0, 96.0)
    st.set_open_price(100.0, "test")
    st.state = S.ENTERED
    st.entry_price, st.stop_price, st.initial_stop, st.qty = 100.0, 99.0, 99.0, 10
    check("untrailed to start", st.trailed(), False)
    check("original stop still breaches", st.stop_breached(98.9), True)
    st.apply_trail(st.trail_target(115.0), 115.0)
    check("now trailed", st.trailed(), True)
    check("the old level no longer matters", st.stop_breached(102.0), True)
    check("above the new stop is safe", st.stop_breached(104.0), False)
    approx("best price tracked for the ledger", st.best_price, 115.0)


def t_time_helpers():
    print("\n[12] The clock")
    config.STRATEGY["open_time"] = "09:15:00"
    config.STRATEGY["check_seconds"] = 30
    config.STRATEGY["last_entry_time"] = "09:28:00"
    config.STRATEGY["square_off_time"] = "09:30:00"
    check("check is 30s after the open",
          (config.check_dt() - config.open_dt()).total_seconds(), 30.0)
    check("last entry is before square off",
          config.last_entry_dt() < config.square_off_dt(), True)
    check("total exposure is fifteen minutes",
          (config.square_off_dt() - config.open_dt()).total_seconds(), 900.0)


# ======================================================================
if __name__ == "__main__":
    print("=" * 62)
    print("POMR offline logic tests")
    print("=" * 62)
    for fn in (t_buffer, t_gainer_path, t_loser_mirror, t_never_tested,
               t_early_check, t_deep_test_disqualified, t_sizing, t_filters,
               t_short_list, t_stop_breach, t_one_entry_per_stock,
               t_trail_off_by_default, t_trail_long, t_trail_short_mirror,
               t_trail_clamp, t_trail_never_widens, t_trail_exit_reason,
               t_time_helpers):
        fn()
    print("\n" + "=" * 62)
    print(f"{PASS} passed, {FAIL} failed")
    print("=" * 62)
    raise SystemExit(1 if FAIL else 0)
