"""
engine.py
=========
Runs the morning.

    09:00        pre-open auction begins (nothing for us to do)
    09:08:30     rank every F&O stock, lock the ten names
    09:14        live feed connected and verified
    09:15:00     market opens, each stock's opening price recorded
    09:15:15/30  the early check
    09:28        last entry
    09:30:00     everything closed

Three threads:
    * the WebSocket callback, which only ever updates state and enqueues
      work — it never places an order, because a REST round trip inside the
      tick handler would stall the feed for every other stock;
    * a worker that drains the action queue and talks to the broker;
    * a one-second monitor for everything the clock drives.
"""

from __future__ import annotations
import queue
import threading
import time
from datetime import datetime

import config
from logger import logger
from angel_connection import connection_manager
from angel_websocket import WebSocketFeed
from order_manager import OrderManager
import angel_data
import scanner
import strategy as S


class TradingEngine:
    def __init__(self, status_cb=None):
        self.status_cb = status_cb
        self.om = OrderManager()
        self.feed = None
        self.states: dict[str, S.StockState] = {}   # name -> state
        self.by_token: dict[str, S.StockState] = {}  # tick routing
        self.opt_token_owner: dict[str, S.StockState] = {}

        self.capital = float(config.STRATEGY["capital"])
        self.trades_taken = 0
        self.halted = False
        self.halt_reason = ""
        self.phase = "idle"

        self._q = queue.Queue()
        self._stop = threading.Event()
        self._threads = []

    # ==================================================================
    # lifecycle
    # ==================================================================
    def start(self):
        self._stop.clear()
        t = threading.Thread(target=self._run, daemon=True, name="pomr-main")
        t.start()
        self._threads.append(t)

    def stop(self):
        logger.info("Stop requested.")
        self._stop.set()
        if self.feed:
            self.feed.stop()

    # ==================================================================
    def _run(self):
        try:
            self._phase("connecting")
            if not connection_manager.connect():
                logger.critical("Cannot continue without a broker session.")
                self._phase("failed")
                return

            self._read_capital()
            self._preflight()

            self._phase("universe")
            if not angel_data.download_scrip_master():
                logger.critical("No scrip master — cannot build the universe.")
                self._phase("failed")
                return
            universe = angel_data.fo_stock_universe()
            if not universe:
                logger.critical("Empty F&O universe.")
                self._phase("failed")
                return

            # ---------------- 09:08:30 : the scan ----------------
            self._phase("waiting for the scan")
            if not self._sleep_until(config.today_at(config.STRATEGY["scan_time"]),
                                     f"the {config.STRATEGY['scan_time']} scan"):
                return

            self._phase("scanning")
            snapshot = angel_data.preopen_snapshot(universe)
            watchlist = scanner.run_scan(snapshot)
            if not watchlist:
                logger.warning("No stock qualified this morning. Nothing to "
                               "trade — this is a normal outcome on a flat day.")
                self._phase("no setups")
                return

            for row in watchlist:
                st = S.StockState(
                    name=row["name"], symbol=row["symbol"], token=row["token"],
                    side=row["side"], rank=row["rank"],
                    tick_size=row["tick_size"], lot_size=row["lot_size"],
                    auction_price=row["auction_price"],
                    prev_close=row["prev_close"], gap_pct=row["gap_pct"])
                self.states[st.name] = st
                self.by_token[st.token] = st
            self._push_status()

            if config.TRADING_MODE == "SCAN_ONLY":
                logger.info("SCAN_ONLY mode — the watchlist is on disk and no "
                            "trading will happen. This is Stage 1 of the build "
                            "plan; run it for ten sessions and check the output "
                            "by hand against the exchange.")
                self._phase("scan complete")
                return

            # ---------------- 09:14 : the feed ----------------
            self._phase("waiting for the feed window")
            if not self._sleep_until(config.today_at(config.STRATEGY["feed_time"]),
                                     "the feed connection window"):
                return

            self._phase("connecting feed")
            self.feed = WebSocketFeed(self._on_tick)
            for st in self.states.values():
                self.feed.add_token(st.token, "NSE")
            self.feed.start()

            for _ in range(20):
                if self.feed.connected or self._stop.is_set():
                    break
                time.sleep(0.5)
            if not self.feed.connected:
                logger.critical("Live feed did NOT connect before the open. "
                                "Stopping — this strategy cannot run blind.")
                self._phase("failed")
                return
            logger.info(f"Feed verified for {self.feed.token_count()} tokens.")

            # ---------------- workers ----------------
            for target, nm in ((self._worker, "pomr-worker"),
                               (self._monitor, "pomr-monitor")):
                th = threading.Thread(target=target, daemon=True, name=nm)
                th.start()
                self._threads.append(th)

            self._phase("waiting for the open")
            if not self._sleep_until(config.open_dt(), "the 09:15 open"):
                return
            logger.info("=" * 62)
            logger.info("MARKET OPEN — recording opening prices.")
            t = config.STRATEGY
            if t.get("trail_enabled"):
                logger.info(f"Trail ON — stop to cost at "
                            f"{t['trail_trigger_pct']}% profit, then "
                            f"{t['trail_move_pct']}% further per "
                            f"{t['trail_step_pct']}% of profit "
                            f"(percentages of the entry price).")
            else:
                logger.info("Trail OFF — stop stays where the test put it.")
            logger.info("=" * 62)
            self._phase("live")

            # the monitor thread drives everything from here
            while not self._stop.is_set():
                if self.phase == "done":
                    break
                time.sleep(0.5)

        except Exception as e:
            logger.critical(f"Engine crashed: {e}", exc_info=True)
            self._phase("crashed")
        finally:
            if self.feed:
                self.feed.stop()

    # ==================================================================
    def _phase(self, p):
        self.phase = p
        logger.info(f"--- phase: {p} ---")
        self._push_status()

    def _sleep_until(self, target: datetime, label: str) -> bool:
        now = datetime.now()
        if now >= target:
            logger.warning(f"{label} has already passed ({target:%H:%M:%S}); "
                           f"continuing immediately.")
            return True
        logger.info(f"Waiting {int((target - now).total_seconds())}s for "
                    f"{label} ({target:%H:%M:%S}).")
        while datetime.now() < target:
            if self._stop.is_set():
                return False
            time.sleep(0.25)
        return True

    def _preflight(self):
        """
        Prove the session can actually trade BEFORE the open.

        Login, quotes and the WebSocket all work on an Angel app that has no
        order permissions, so the first sign of trouble is an entry failing
        at 09:15 with the session already half over. Reading the order book
        exercises the same permission the order path needs, at 08:11 instead.
        """
        if config.TRADING_MODE != "LIVE":
            return
        try:
            api_rate_limiter.wait("orderBook")
            resp = config.SMART.orderBook()
            if resp is None or (isinstance(resp, dict)
                                and resp.get("status") is False):
                raise RuntimeError(str(resp)[:300])
            n = len((resp or {}).get("data") or [])
            logger.info(f"Pre-flight: order permissions OK "
                        f"({n} order(s) in today's book).")
        except Exception as e:
            logger.critical("PRE-FLIGHT FAILED — this session can read prices "
                            "but may not be able to place orders. Check that "
                            "the Angel app is a TRADING API app and that the "
                            "API key belongs to it. Details: " + str(e)[:300])

    def _read_capital(self):
        if config.STRATEGY["use_live_balance"]:
            bal = angel_data.available_balance()
            if bal and bal > 0:
                self.capital = bal
                logger.info(f"Risk sized off the live balance: "
                            f"Rs {self.capital:,.2f}")
                return
            logger.warning("Live balance unavailable; falling back to the "
                           "configured capital figure.")
        self.capital = float(config.STRATEGY["capital"])
        logger.info(f"Risk sized off the configured capital: "
                    f"Rs {self.capital:,.2f}")

    # ==================================================================
    # tick path (WebSocket thread — must stay fast)
    # ==================================================================
    def _on_tick(self, token, px, day_open, high, low, vol, ts):
        st = self.by_token.get(token)
        if st is not None:
            self._on_stock_tick(st, px, day_open, ts)
            return
        owner = self.opt_token_owner.get(token)
        if owner is not None:
            owner.instrument["ltp"] = px

    def _on_stock_tick(self, st, px, day_open, ts):
        open_dt = config.open_dt()

        # Pre-open prints must never be mistaken for an excursion.
        if ts < open_dt:
            if day_open > 0 and not st.open_price:
                pass  # stale field before the open; ignore
            return

        if not st.open_price:
            if day_open > 0:
                st.set_open_price(day_open, "feed open_price_of_the_day")
            elif (ts - open_dt).total_seconds() > 5:
                # the field never populated — fall back, loudly
                st.set_open_price(px, "first tick after 09:15 (FALLBACK)")
            else:
                return

        signal = st.on_tick(px, ts)

        if st.state == S.ENTERED:
            # The trail is evaluated first so a stop raised on this tick is
            # the one the breach check below uses. The engine moves its own
            # stop immediately and is authoritative; the broker amendment is
            # handed to the worker because it is a REST round trip.
            new_stop = st.trail_target(px)
            if new_stop is not None:
                logger.info(f"{st.name}: {st.apply_trail(new_stop, px)}")
                self._enqueue(("TRAIL", st.name, None))
            else:
                st.best_price = (max(st.best_price or px, px) if st.is_long
                                 else min(st.best_price or px, px))
            if st.stop_breached(px):
                self._enqueue(("EXIT", st.name,
                               "TRAIL_HIT" if st.trailed() else "STOP_HIT"))
            return

        if signal == "ENTER" and not self.halted:
            if datetime.now() < config.check_dt():
                return          # no order may be placed before the check
            if datetime.now() > config.last_entry_dt():
                return          # too little time left before the 09:30 exit
            self._enqueue(("ENTER", st.name, None))

    def _enqueue(self, item):
        kind, name, _ = item
        st = self.states.get(name)
        if st is None:
            return
        flag = f"_pending_{kind}"
        if getattr(st, flag, False):
            return
        setattr(st, flag, True)
        self._q.put(item)

    # ==================================================================
    # worker (all broker calls happen here)
    # ==================================================================
    def _worker(self):
        while not self._stop.is_set():
            try:
                kind, name, arg = self._q.get(timeout=0.3)
            except queue.Empty:
                continue
            st = self.states.get(name)
            if st is None:
                continue
            try:
                if kind == "ENTER":
                    self._do_entry(st)
                elif kind == "EXIT":
                    self._do_exit(st, arg)
                elif kind == "TRAIL":
                    self._do_trail(st)
            except Exception as e:
                logger.critical(f"{name}: {kind} failed: {e}", exc_info=True)
            finally:
                setattr(st, f"_pending_{kind}", False)

    # ------------------------------------------------------------------
    def _do_entry(self, st):
        if st.state != S.TESTED or self.halted:
            return
        now = datetime.now()
        if now > config.last_entry_dt():
            st.state = S.MISSED
            st.note = "crossing came after the last-entry time"
            return

        # ---- daily limits ----
        if self.trades_taken >= int(config.STRATEGY["max_trades"]):
            st.state = S.SKIPPED
            st.note = "daily trade limit reached"
            logger.warning(f"{st.name}: skipped — {st.note}")
            return
        open_now = sum(1 for x in self.states.values() if x.state == S.ENTERED)
        if open_now >= int(config.STRATEGY["max_positions"]):
            logger.warning(f"{st.name}: {open_now} positions already open; "
                           f"this setup waits for a slot.")
            return          # stays TESTED — may still fire if a slot frees up

        entry_ref = st.ltp
        raw_stop = st.build_stop()

        # ---- guard rail: the test ran too deep ----
        depth = st.test_depth_pct()
        if depth > float(config.STRATEGY["max_test_pct"]):
            st.state = S.DISQUALIFIED
            st.note = f"test depth {depth:.2f}% over the cap"
            logger.warning(f"{st.name}: skipped — {st.note}")
            return

        qty, stop_used, risk, note = st.size(entry_ref, raw_stop, self.capital)
        if qty < 1:
            st.state = S.SKIPPED
            st.note = "size worked out below one share"
            logger.warning(f"{st.name}: skipped — {st.note}")
            return

        inst = self._choose_instrument(st, entry_ref, stop_used, qty)
        if inst is None:
            st.state = S.SKIPPED
            st.note = st.note or "no tradeable instrument"
            return
        qty = inst.pop("_qty", qty)
        ref = inst.pop("_ref_price", entry_ref)

        logger.info(f"{st.name}: ENTRY SIGNAL — {'BUY' if st.is_long else 'SHORT'} "
                    f"{qty} {inst['symbol']} @ ~{ref:.2f}, stop {stop_used:.2f} "
                    f"(test extreme {st.test_extreme:.2f}, depth {depth:.2f}%), "
                    f"risk Rs {risk:,.0f}{'; ' + note if note else ''}")

        fill = self.om.enter(st, inst, qty, ref)
        if fill is None:
            st.state = S.SKIPPED
            st.note = "entry order failed"
            return

        st.entry_price = fill
        st.entry_time = datetime.now()
        st.qty = qty
        st.stop_price = stop_used
        st.initial_stop = stop_used
        st.risk_amount = risk
        st.instrument = inst
        st.note = note
        st.state = S.ENTERED
        self.trades_taken += 1

        if inst["kind"] == "STOCK" or inst["kind"] == "FUTURE":
            self.om.place_stop(st, inst, qty, stop_used)
        else:
            logger.warning(f"{st.name}: option leg — the stop is a STOCK price "
                           f"level, so it cannot rest with the broker. The "
                           f"engine is the only cover on this position.")

        self._push_status()

    # ------------------------------------------------------------------
    def _choose_instrument(self, st, entry_ref, stop, qty) -> dict | None:
        """Stock by default. Option or future where configured and viable."""
        s = config.STRATEGY
        stock = {"kind": "STOCK", "symbol": st.symbol, "token": st.token,
                 "exchange": "NSE", "tick_size": st.tick_size,
                 "_qty": qty, "_ref_price": entry_ref}

        # ---- short-side fallback to futures ----
        if (not st.is_long) and s["short_instrument"] == "FUTURES":
            fut = angel_data.resolve_future(st.name)
            if fut and fut["lot_size"] > 0:
                dist = abs(entry_ref - stop)
                risk_per_lot = fut["lot_size"] * dist
                budget = self.capital * float(s["risk_pct"]) / 100.0
                lots = int(budget // risk_per_lot) if risk_per_lot > 0 else 0
                if lots >= 1:
                    fut["_qty"] = lots * fut["lot_size"]
                    fut["_ref_price"] = entry_ref
                    logger.info(f"{st.name}: short routed to FUTURES "
                                f"{fut['symbol']} ({lots} lot(s))")
                    self._subscribe_extra(fut["token"], st)
                    return fut
                logger.warning(f"{st.name}: one future lot risks more than the "
                               f"budget; falling back to the cash short.")
            else:
                logger.warning(f"{st.name}: no future found; cash short it is.")

        if s["instrument"] != "OPTION":
            return stock

        # ---- option route ----
        side = "CE" if st.is_long else "PE"
        opt = angel_data.resolve_option(st.name, st.open_price, side)
        if not opt or opt["lot_size"] <= 0:
            logger.warning(f"{st.name}: no ATM {side}; falling back to the stock.")
            return stock

        q = angel_data.option_quote(opt["token"])
        ok, why = self._option_liquid(q)
        if not ok:
            logger.warning(f"{st.name}: option {opt['symbol']} failed the "
                           f"liquidity check ({why}); falling back to the "
                           f"stock. The signal is still valid — only the "
                           f"instrument was unavailable.")
            return stock

        premium = q["ask"] or q["ltp"]
        dist = abs(entry_ref - stop)
        delta = float(s.get("option_delta_est", 0.5))
        # Loss if the stock reaches its stop, floored by the premium itself.
        risk_per_lot = opt["lot_size"] * min(premium, delta * dist)
        budget = self.capital * float(s["risk_pct"]) / 100.0
        lots = int(budget // risk_per_lot) if risk_per_lot > 0 else 0

        max_value = self.capital * float(s["max_position_pct"]) / 100.0
        lots = min(lots, int(max_value // (opt["lot_size"] * premium))
                   if premium > 0 else 0)

        if lots < 1:
            logger.warning(f"{st.name}: one option lot already risks more than "
                           f"{s['risk_pct']}% of the account. We do not stretch "
                           f"the budget to fit the lot — falling back to the "
                           f"stock.")
            return stock

        opt["_qty"] = lots * opt["lot_size"]
        opt["_ref_price"] = premium
        opt["stock_stop"] = stop
        logger.info(f"{st.name}: routed to OPTION {opt['symbol']} "
                    f"({lots} lot(s) x {opt['lot_size']} @ ~{premium:.2f})")
        self._subscribe_extra(opt["token"], st)
        return opt

    def _option_liquid(self, q) -> tuple[bool, str]:
        s = config.STRATEGY
        if not q:
            return False, "no quote"
        if q["bid"] <= 0 or q["ask"] <= 0:
            return False, "one side of the book is not quoted"
        mid = (q["bid"] + q["ask"]) / 2.0
        if mid <= 0:
            return False, "no mid"
        spread = (q["ask"] - q["bid"]) / mid * 100.0
        if spread > float(s["option_max_spread_pct"]):
            return False, f"spread {spread:.2f}% over cap"
        if q["oi"] < float(s["option_min_oi"]):
            return False, f"open interest {q['oi']:.0f} under floor"
        if s["option_require_traded"] and q["volume"] <= 0:
            return False, "has not traded today"
        return True, ""

    def _subscribe_extra(self, token, owner):
        if not self.feed:
            return
        self.opt_token_owner[str(token)] = owner
        self.feed.add_token(str(token), "NFO")
        self.feed.resubscribe()

    # ------------------------------------------------------------------
    def _do_trail(self, st):
        """Amend the resting stop. Takes the same lock as the exit so a trail
        can never be sent against a position that is being closed."""
        if st.state != S.ENTERED:
            return
        inst = st.instrument
        if inst.get("kind") == "OPTION":
            # The stop is a stock price level, so there is nothing resting at
            # the broker to amend. The engine trail still applies.
            return
        lock = self.om.lock_for(st.name)
        if not lock.acquire(blocking=False):
            return
        try:
            if st.state != S.ENTERED:
                return
            self.om.modify_stop(st, inst, st.qty, st.stop_price)
        finally:
            lock.release()

    # ------------------------------------------------------------------
    def _do_exit(self, st, reason):
        lock = self.om.lock_for(st.name)
        if not lock.acquire(blocking=False):
            return
        try:
            if st.state != S.ENTERED:
                return
            inst = st.instrument
            ref = inst.get("ltp") or st.ltp
            if inst["kind"] == "STOCK" or inst["kind"] == "FUTURE":
                ref = st.ltp

            fill, resolved, ok = self.om.exit(st, inst, st.qty, ref, reason)
            if not ok:
                # The broker did not confirm. Leave the position OPEN in the
                # app so it keeps being managed and keeps being retried, and
                # so nothing phantom reaches the trade log.
                st.exit_attempts = getattr(st, "exit_attempts", 0) + 1
                if st.exit_attempts >= 3:
                    logger.critical(f"{st.name}: {st.exit_attempts} exit "
                                    f"attempts have failed. Backing off until "
                                    f"the square-off sweep. CLOSE THIS "
                                    f"MANUALLY IF IT IS STILL OPEN.")
                return

            st.exit_price = fill
            st.exit_time = datetime.now()
            st.exit_reason = resolved
            if inst["kind"] == "OPTION":
                st.pnl = (fill - st.entry_price) * st.qty        # always bought
            elif st.is_long:
                st.pnl = (fill - st.entry_price) * st.qty
            else:
                st.pnl = (st.entry_price - fill) * st.qty
            st.state = S.CLOSED
            self.om.book_pnl(st)
            self.om.log_trade(st, self.capital)
            self._push_status()
        finally:
            lock.release()

    # ==================================================================
    # monitor (the clock)
    # ==================================================================
    def _monitor(self):
        announced_check = False
        announced_last = False
        last_status = 0.0
        last_sl_poll = 0.0

        while not self._stop.is_set():
            now = datetime.now()

            if not announced_check and now >= config.check_dt():
                announced_check = True
                ready = [s.name for s in self.states.values()
                         if s.state == S.TESTED]
                logger.info(f"THE CHECK ({config.STRATEGY['check_seconds']}s "
                            f"after the open) — {len(ready)} stock(s) already "
                            f"out and back: {ready if ready else 'none'}")

            if not announced_last and now >= config.last_entry_dt():
                announced_last = True
                logger.info("Last entry time passed — no new positions. "
                            "Managing what is open until 09:30.")
                for s in self.states.values():
                    if s.state in (S.WAITING_TEST, S.TESTED):
                        s.state = S.MISSED
                        s.note = s.note or "no reclaim before the last-entry time"

            # ---- daily loss cap ----
            if not self.halted:
                total = self.om.realized + sum(s.unrealized()
                                               for s in self.states.values())
                cap = -abs(self.capital * float(config.STRATEGY["daily_loss_cap_pct"]) / 100.0)
                if total <= cap:
                    self.halted = True
                    self.halt_reason = (f"daily loss cap hit "
                                        f"(Rs {total:,.0f} vs Rs {cap:,.0f})")
                    logger.critical(f"DAILY LOSS CAP — {self.halt_reason}. "
                                    f"Closing everything and stopping.")
                    self._close_all("LOSS_CAP")

            # ---- resting stops that fired without us ----
            if config.TRADING_MODE == "LIVE" and time.time() - last_sl_poll > 3:
                last_sl_poll = time.time()
                for s in list(self.states.values()):
                    if s.state == S.ENTERED and self.om.sl_orders.get(s.name):
                        filled, px = self.om.stop_filled(s.name)
                        if filled:
                            logger.info(f"{s.name}: resting stop filled at "
                                        f"{px:.2f}; booking it.")
                            s.ltp = px or s.ltp
                            self._enqueue(("EXIT", s.name,
                                           "TRAIL_HIT" if s.trailed()
                                           else "STOP_HIT"))

            # ---- 09:30, win or lose ----
            if now >= config.square_off_dt():
                logger.info("=" * 62)
                logger.info("09:30 — TIME IS UP. Closing everything.")
                logger.info("=" * 62)
                self._close_all("TIME_EXIT")
                self._wait_flat()
                self._verify_flat()
                self._end_of_day()
                return

            if time.time() - last_status > 1.0:
                last_status = time.time()
                self._push_status()

            time.sleep(0.25)

    def _close_all(self, reason):
        for s in self.states.values():
            if s.state == S.ENTERED:
                if reason != "TIME_EXIT" and getattr(s, "exit_attempts", 0) >= 3:
                    continue          # backed off; the square-off sweep retries
                self._enqueue(("EXIT", s.name, reason))

    def _verify_flat(self):
        """Ask the broker what it thinks we are holding. The app believing it
        is flat is not the same as being flat."""
        if config.TRADING_MODE != "LIVE":
            return
        pos = self.om.fetch_positions()
        if pos is None:
            logger.warning("Could not read positions from the broker — verify "
                           "in the terminal that everything is squared off.")
            return
        ours = {s.instrument.get("symbol") for s in self.states.values()
                if s.instrument}
        live = {sym: q for sym, q in pos.items() if q and sym in ours}
        if live:
            logger.critical(f"!!! BROKER STILL SHOWS OPEN QUANTITY: {live}. "
                            f"SQUARE OFF MANUALLY NOW. !!!")
        else:
            logger.info("Broker positions confirm flat.")

    def _wait_flat(self, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not any(s.state == S.ENTERED for s in self.states.values()):
                return
            time.sleep(0.5)
        still = [s.name for s in self.states.values() if s.state == S.ENTERED]
        if still:
            logger.critical(f"!!! STILL SHOWING OPEN after the square-off "
                            f"window: {still}. CHECK THE TERMINAL NOW. !!!")

    def _end_of_day(self):
        for s in self.states.values():
            if s.state in (S.WAITING_OPEN, S.WAITING_TEST, S.TESTED):
                s.state = S.MISSED
                s.note = s.note or "pattern never completed"

        summ = self.om.summary()
        logger.info("=" * 62)
        logger.info(f"SESSION DONE  |  trades {summ['trades']}  |  "
                    f"realized Rs {summ['realized']:,.2f}  |  "
                    f"{summ['realized'] / self.capital * 100:.2f}% of capital")
        for s in self.states.values():
            logger.info(f"  {s.name:<14} {s.side:<7} {s.state:<13} "
                        f"{s.note}")
        logger.info(f"Trade log:  {config.trades_file()}")
        logger.info(f"Order log:  {config.orders_file()}")
        logger.info(f"Watchlist:  {config.scan_file()}")
        logger.info("=" * 62)
        self.phase = "done"
        self._push_status()
        if self.feed:
            self.feed.stop()

    # ==================================================================
    def _push_status(self):
        if self.status_cb:
            try:
                self.status_cb(self.live_snapshot())
            except Exception:
                pass

    def live_snapshot(self) -> dict:
        rows = [s.snapshot() for s in self.states.values()]
        rows.sort(key=lambda r: (r["side"] != "LONG", -abs(r["gap_pct"])))
        unreal = sum(s.unrealized() for s in self.states.values())
        return {
            "phase": self.phase,
            "mode": config.TRADING_MODE,
            "capital": self.capital,
            "realized": self.om.realized,
            "unrealized": unreal,
            "total": self.om.realized + unreal,
            "trades_taken": self.trades_taken,
            "max_trades": int(config.STRATEGY["max_trades"]),
            "open_positions": sum(1 for s in self.states.values()
                                  if s.state == S.ENTERED),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "feed": bool(self.feed and self.feed.connected),
            "ticks": self.feed.tick_count if self.feed else 0,
            "rows": rows,
        }
