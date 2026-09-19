"""
order_manager.py
================
Executes entries, protective stops and exits in PAPER or LIVE mode, and owns
both CSV ledgers.

There are exactly two exits and no others: the stop is hit, or the clock
reaches 09:30. No profit target, no trailing stop, no partial exits.

The protective stop is a resting order with the broker from the moment we
are filled, so it works even if our connection drops. That creates the one
genuinely dangerous case in a strategy this fast: the exchange stop fills
while the engine still believes it is in the position, and the engine then
sends a second exit. Every exit path therefore resolves the resting stop
first — checks whether it already filled, and only sends a market order if
it did not.

LIVE cash shorts are intraday-only and squared off the same day, which ours
always are. Some brokers still block shorting on individual scrips at short
notice, so a rejection is detected and reported rather than assumed away;
the futures fallback is available per the strategy note.
"""

from __future__ import annotations
import csv
import os
import time
import threading
from datetime import datetime

import config
from logger import logger
from api_rate_limiter import api_rate_limiter
from angel_data import round_to_tick
import cautionary

_ORDER_COLS = ["time", "mode", "name", "instrument", "symbol", "side",
               "qty", "price", "order_type", "order_id", "status", "detail"]

_TRADE_COLS = ["date", "name", "side", "instrument", "symbol", "gap_pct",
               "open_price", "test_extreme", "test_depth_pct",
               "entry_time", "entry_price", "stop_price", "qty",
               "risk_amount", "exit_time", "exit_price", "exit_reason",
               "pnl", "pnl_pct_of_capital", "hold_seconds",
               "initial_stop", "final_stop", "trail_steps", "best_price",
               "best_excursion", "note"]


class OrderManager:
    def __init__(self):
        self.realized = 0.0
        self.trades = []                 # completed round trips
        self.sl_orders = {}              # name -> resting SL order id
        self._locks = {}                 # name -> lock (one exit at a time)
        self._ensure_csv()

    # ------------------------------------------------------------------
    # ledgers
    # ------------------------------------------------------------------
    def _ensure_csv(self):
        for path, cols in ((config.orders_file(), _ORDER_COLS),
                           (config.trades_file(), _TRADE_COLS)):
            if not os.path.exists(path):
                with open(path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(cols)

    def _log_order(self, name, inst, side, qty, price, otype,
                   order_id="", status="", detail=""):
        row = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "mode": config.TRADING_MODE,
            "name": name,
            "instrument": inst.get("kind", "STOCK"),
            "symbol": inst.get("symbol", ""),
            "side": side, "qty": qty, "price": round(float(price), 2),
            "order_type": otype, "order_id": order_id or "",
            "status": status, "detail": detail,
        }
        with open(config.orders_file(), "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_ORDER_COLS).writerow(row)

    def log_trade(self, st, capital: float):
        """One row per completed round trip."""
        hold = 0
        if st.entry_time and st.exit_time:
            hold = int((st.exit_time - st.entry_time).total_seconds())
        row = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "name": st.name,
            "side": "LONG" if st.is_long else "SHORT",
            "instrument": st.instrument.get("kind", "STOCK"),
            "symbol": st.instrument.get("symbol", st.symbol),
            "gap_pct": st.gap_pct,
            "open_price": st.open_price,
            "test_extreme": st.test_extreme,
            "test_depth_pct": round(st.test_depth_pct(), 3),
            "entry_time": st.entry_time.strftime("%H:%M:%S") if st.entry_time else "",
            "entry_price": round(st.entry_price, 2),
            "stop_price": round(st.initial_stop, 2),
            "qty": st.qty,
            "risk_amount": round(st.risk_amount, 2),
            "exit_time": st.exit_time.strftime("%H:%M:%S") if st.exit_time else "",
            "exit_price": round(st.exit_price, 2),
            "exit_reason": st.exit_reason,
            "pnl": round(st.pnl, 2),
            "pnl_pct_of_capital": round(st.pnl / capital * 100.0, 4) if capital else 0.0,
            "hold_seconds": hold,
            "initial_stop": round(st.initial_stop, 2),
            "final_stop": round(st.stop_price, 2),
            "trail_steps": st.trail_steps,
            "best_price": round(st.best_price, 2) if st.best_price else "",
            "best_excursion": (round(st.open_profit(st.best_price), 2)
                               if st.best_price else ""),
            "note": st.note,
        }
        self.trades.append(row)
        with open(config.trades_file(), "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_TRADE_COLS).writerow(row)
        logger.info(f"TRADE LOGGED | {st.name} {row['side']} qty {st.qty} "
                    f"{st.entry_price:.2f} -> {st.exit_price:.2f} "
                    f"({st.exit_reason})  P&L Rs {st.pnl:,.2f}")

    def lock_for(self, name):
        return self._locks.setdefault(name, threading.Lock())

    # ------------------------------------------------------------------
    # entry
    # ------------------------------------------------------------------
    def enter(self, st, inst: dict, qty: int, ref_price: float) -> float | None:
        """Market entry. Buy the gainer, sell the loser short."""
        side = "BUY" if st.is_long else "SELL"
        if config.TRADING_MODE == "PAPER":
            px = self._paper_fill(ref_price, side)
            self._log_order(st.name, inst, side, qty, px, "MARKET",
                            status="PAPER_FILL")
            logger.info(f"[PAPER] {side} {qty} {inst['symbol']} @ {px:.2f}")
            return px

        oid = self._send(self._market_params(inst, qty, side),
                         f"ENTRY {side} {qty} {inst['symbol']}", st.name)
        self._log_order(st.name, inst, side, qty, ref_price, "MARKET",
                        order_id=oid or "", status="SUBMITTED")
        ok, fill, detail = self._verify(oid, f"ENTRY {side} {inst['symbol']}")
        self._log_order(st.name, inst, side, qty, fill or ref_price, "MARKET",
                        order_id=oid or "",
                        status="COMPLETE" if ok else "FAILED", detail=detail)
        if not ok:
            logger.critical(f"ENTRY FAILED for {st.name} — no position taken. "
                            f"{detail}")
            return None
        return fill or ref_price

    # ------------------------------------------------------------------
    # protective stop
    # ------------------------------------------------------------------
    def place_stop(self, st, inst: dict, qty: int, trigger: float):
        """One resting stop per name. Never stacks."""
        if config.TRADING_MODE != "LIVE" or not config.STRATEGY["place_exchange_sl"]:
            return None
        if self.sl_orders.get(st.name):
            logger.warning(f"{st.name}: a resting stop already exists; "
                           f"not placing a second one.")
            return self.sl_orders[st.name]

        tick = inst.get("tick_size") or st.tick_size or 0.05
        side = "SELL" if st.is_long else "BUY"
        # A SELL stop needs its limit below the trigger, a BUY stop above it,
        # or the order can trigger and then never fill.
        trig = round_to_tick(trigger, tick, "down" if st.is_long else "up")
        limit = round_to_tick(trig - 3 * tick if st.is_long else trig + 3 * tick,
                              tick, "down" if st.is_long else "up")

        params = {
            "variety": "STOPLOSS",
            "tradingsymbol": inst["symbol"],
            "symboltoken": str(inst["token"]),
            "transactiontype": side,
            "exchange": inst["exchange"],
            "ordertype": "STOPLOSS_LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": f"{limit:.2f}",
            "triggerprice": f"{trig:.2f}",
            "quantity": str(qty),
        }
        oid = self._send(params, f"STOP {side} {qty} {inst['symbol']} @ {trig}",
                         st.name)
        self._log_order(st.name, inst, side, qty, trig, "STOPLOSS_LIMIT",
                        order_id=oid or "",
                        status="RESTING" if oid else "FAILED")
        if oid and self._verify_sl_rests(oid, trig):
            self.sl_orders[st.name] = oid
            st.stop_sent = trig
            logger.info(f"{st.name}: protective stop resting at {trig:.2f} "
                        f"(limit {limit:.2f}), id={oid} — verified at exchange")
        elif oid:
            # Placed but not resting. A dead order is not protection.
            logger.critical(f"{st.name}: protective stop {oid} did NOT rest at "
                            f"{trig:.2f} — position UNPROTECTED at the broker, "
                            f"CHECK THE TERMINAL. The engine stop is the only "
                            f"cover.")
        else:
            logger.critical(f"{st.name}: PROTECTIVE STOP NOT PLACED. The "
                            f"position is unprotected at the broker — the "
                            f"engine stop is the only cover.")
        return oid

    def modify_stop(self, st, inst: dict, qty: int, trigger: float) -> bool:
        """
        Amend the resting stop to a new trigger.

        Deliberately a modify and not a cancel-then-place: cancelling leaves
        the position naked for the round trip, and inside a fifteen-minute
        strategy that window is a real exposure rather than a theoretical one.
        """
        if config.TRADING_MODE != "LIVE" or not config.STRATEGY["place_exchange_sl"]:
            return True                      # engine-side trail only
        oid = self.sl_orders.get(st.name)
        if not oid:
            logger.warning(f"{st.name}: no resting stop to modify; the engine "
                           f"stop is the only cover.")
            return False

        tick = inst.get("tick_size") or st.tick_size or 0.05
        trig = round_to_tick(trigger, tick, "down" if st.is_long else "up")
        limit = round_to_tick(trig - 3 * tick if st.is_long else trig + 3 * tick,
                              tick, "down" if st.is_long else "up")

        params = {
            "variety": "STOPLOSS",
            "orderid": str(oid),
            "tradingsymbol": inst["symbol"],
            "symboltoken": str(inst["token"]),
            "exchange": inst["exchange"],
            "ordertype": "STOPLOSS_LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": f"{limit:.2f}",
            "triggerprice": f"{trig:.2f}",
            "quantity": str(qty),
        }
        for attempt in range(2):
            try:
                api_rate_limiter.wait("modifyOrder")
                config.SMART.modifyOrder(params)
                # A returning call is not proof the exchange took it. Read it
                # back before believing the stop has moved.
                if self._verify_sl_rests(oid, trig):
                    st.stop_sent = trig
                    self._log_order(st.name, inst,
                                    "SELL" if st.is_long else "BUY", qty, trig,
                                    "STOPLOSS_LIMIT", order_id=str(oid),
                                    status="MODIFIED", detail="trail verified")
                    logger.info(f"{st.name}: resting stop moved to {trig:.2f} "
                                f"(limit {limit:.2f}) — verified")
                    return True
                logger.warning(f"{st.name}: modify did not take at the exchange "
                               f"(wanted {trig:.2f}); replacing the stop.")
                return self._replace_stop(st, inst, qty, trig, limit)
            except Exception as e:
                msg = str(e)
                logger.error(f"{st.name}: modify stop attempt {attempt+1} "
                             f"failed: {msg}")
                # If it already triggered there is nothing left to modify —
                # the monitor will pick the fill up on its next poll.
                if "not open" in msg.lower() or "cannot" in msg.lower():
                    break
                time.sleep(0.6)

        self._log_order(st.name, inst, "SELL" if st.is_long else "BUY", qty,
                        trigger, "STOPLOSS_LIMIT", order_id=str(oid),
                        status="MODIFY_FAILED", detail="trail")
        logger.critical(f"{st.name}: TRAIL NOT APPLIED AT THE BROKER. The "
                        f"resting stop is still at {st.stop_sent or st.initial_stop:.2f} "
                        f"while the engine is working {trigger:.2f}.")
        return False

    def _replace_stop(self, st, inst, qty, trig, limit) -> bool:
        """Cancel the tracked stop and place a fresh one at the wanted trigger,
        then verify. Used only when a modify silently fails."""
        self.cancel_stop(st.name)
        oid = self._send({
            "variety": "STOPLOSS",
            "tradingsymbol": inst["symbol"],
            "symboltoken": str(inst["token"]),
            "transactiontype": "SELL" if st.is_long else "BUY",
            "exchange": inst["exchange"],
            "ordertype": "STOPLOSS_LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": f"{limit:.2f}",
            "triggerprice": f"{trig:.2f}",
            "quantity": str(qty),
        }, f"REPLACE STOP {inst['symbol']} @ {trig}")
        if oid and self._verify_sl_rests(oid, trig):
            self.sl_orders[st.name] = oid
            st.stop_sent = trig
            logger.info(f"{st.name}: stop replaced -> id {oid} @ {trig:.2f} "
                        f"(verified)")
            return True
        if oid:
            self.sl_orders[st.name] = oid
        logger.critical(f"{st.name}: could not re-establish the resting stop at "
                        f"{trig:.2f} — position may be UNPROTECTED at the "
                        f"broker, CHECK THE TERMINAL.")
        return False

    def stop_filled(self, name) -> tuple[bool, float]:
        """Has the resting stop already done its job? Returns (filled, price)."""
        oid = self.sl_orders.get(name)
        if not oid or config.TRADING_MODE != "LIVE":
            return False, 0.0
        status, price, _ = self._order_status(oid)
        if status in ("complete", "filled"):
            return True, price
        return False, 0.0

    def cancel_stop(self, name):
        oid = self.sl_orders.pop(name, None)
        if not oid or config.TRADING_MODE != "LIVE":
            return
        try:
            api_rate_limiter.wait("cancelOrder")
            config.SMART.cancelOrder(oid, "STOPLOSS")
            logger.info(f"{name}: cancelled resting stop {oid}")
        except Exception as e:
            logger.error(f"{name}: cancel stop {oid} failed: {e}")

    # ------------------------------------------------------------------
    # exit
    # ------------------------------------------------------------------
    def exit(self, st, inst: dict, qty: int, ref_price: float,
             reason: str) -> tuple[float, str, bool]:
        """
        Close the position. Returns (fill_price, resolved_reason, ok).

        ok is False when the broker did not confirm the exit. The caller MUST
        NOT book the trade in that case: the position is still open at the
        broker, and recording a phantom close would put the app's P&L and its
        position state permanently out of step with reality.

        Resolves the resting stop first so we can never send a second sell
        against a position the exchange has already closed.
        """
        side = "SELL" if st.is_long else "BUY"

        if config.TRADING_MODE == "LIVE":
            filled, fill_px = self.stop_filled(st.name)
            if filled:
                self.sl_orders.pop(st.name, None)
                logger.info(f"{st.name}: resting stop had already filled at "
                            f"{fill_px:.2f} — no exit order sent.")
                self._log_order(st.name, inst, side, qty, fill_px,
                                "STOPLOSS_LIMIT", status="COMPLETE",
                                detail="resting stop filled")
                return fill_px, ("TRAIL_HIT" if st.trailed() else "STOP_HIT"), True
            self.cancel_stop(st.name)

            oid = self._send(self._market_params(inst, qty, side),
                             f"EXIT {side} {qty} {inst['symbol']} ({reason})",
                             st.name)
            self._log_order(st.name, inst, side, qty, ref_price, "MARKET",
                            order_id=oid or "", status="SUBMITTED",
                            detail=reason)
            ok, fill, detail = self._verify(oid, f"EXIT {inst['symbol']}")
            self._log_order(st.name, inst, side, qty, fill or ref_price,
                            "MARKET", order_id=oid or "",
                            status="COMPLETE" if ok else "FAILED",
                            detail=f"{reason}; {detail}")
            if not ok:
                logger.critical(f"!!! EXIT NOT CONFIRMED for {st.name} "
                                f"({reason}) — NOT booked. The position is "
                                f"still OPEN at the broker. CHECK THE "
                                f"TERMINAL. {detail} !!!")
                return 0.0, reason, False
            return (fill or ref_price), reason, True

        px = self._paper_fill(ref_price, side)
        self._log_order(st.name, inst, side, qty, px, "MARKET",
                        status="PAPER_FILL", detail=reason)
        logger.info(f"[PAPER] {side} {qty} {inst['symbol']} @ {px:.2f} ({reason})")
        return px, reason, True

    def book_pnl(self, st):
        self.realized += st.pnl

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _paper_fill(self, ref: float, side: str) -> float:
        slip = float(config.STRATEGY["paper_slippage_pct"]) / 100.0
        return round(ref * (1 + slip) if side == "BUY" else ref * (1 - slip), 2)

    @staticmethod
    def _market_params(inst, qty, side):
        return {
            "variety": "NORMAL",
            "tradingsymbol": inst["symbol"],
            "symboltoken": str(inst["token"]),
            "transactiontype": side,
            "exchange": inst["exchange"],
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": "0", "squareoff": "0", "stoploss": "0",
            "quantity": str(qty),
        }

    def _send(self, params, label, name: str = ""):
        """
        Submit an order and, crucially, surface WHY it was refused.

        SmartAPI's placeOrder() returns the order id on success and plain
        None on failure — the broker's message and error code are discarded
        inside the SDK before we ever see them. placeOrderFullResponse()
        hands back the whole envelope, so a rejection tells us what actually
        happened instead of "bad id: None". We fall back to placeOrder() only
        where the SDK is too old to have it.
        """
        for attempt in range(config.RETRY["max_retries"]):
            try:
                api_rate_limiter.wait("placeOrder")
                oid, detail = self._submit(params)
                if oid and len(str(oid)) > 4:
                    logger.info(f"Order submitted [{label}] id={oid}")
                    return str(oid)
                logger.error(f"Order refused [{label}] attempt {attempt+1}: "
                             f"{detail or 'no id and no message from the broker'}")
                if self._fatal_reject(detail, label, name):
                    return None
            except Exception as e:
                msg = str(e)
                logger.error(f"Order error [{label}] attempt {attempt+1}: {msg}")
                if self._fatal_reject(msg, label, name):
                    return None
            time.sleep([1, 2, 4][min(attempt, 2)])
        logger.critical(f"Order FAILED after retries [{label}]")
        return None

    @staticmethod
    def _submit(params) -> tuple[str | None, str]:
        """Returns (order_id, detail). detail carries the broker's own words."""
        obj = config.SMART
        full = getattr(obj, "placeOrderFullResponse", None)
        if callable(full):
            resp = full(params) or {}
            data = resp.get("data") or {}
            oid = data.get("orderid") or data.get("uniqueorderid")
            if oid:
                return str(oid), ""
            bits = [str(resp.get("message") or "").strip(),
                    str(resp.get("errorcode") or "").strip()]
            detail = " | ".join(b for b in bits if b) or str(resp)[:300]
            return None, detail
        # older SDK: no message available, only the id or None
        return obj.placeOrder(params), ""

    @staticmethod
    def _fatal_reject(detail: str, label: str, name: str = "") -> bool:
        """True when retrying cannot possibly help. Each of these is a
        configuration, permission or regulatory problem, not a transient one."""
        d = (detail or "").lower()
        if not d:
            return False
        # Exchange surveillance: Angel blocks these from placeOrder in the
        # EQUITY segment only. Retrying is pointless — it will be refused
        # every time, today and tomorrow — so record it and move on.
        if "ab4036" in d or "cautionary" in d or "surveillance" in d:
            if name:
                cautionary.record(name)
            else:
                logger.critical("Order refused: the scrip is under exchange "
                                "surveillance and cannot be traded through the "
                                "API in the cash segment.")
            return True
        if ("short" in d and ("not allow" in d or "block" in d)):
            logger.critical("Broker rejected the short on this scrip. Switch "
                            "'Short side via' to FUTURES for this name.")
            return True
        if any(k in d for k in ("invalid api", "access denied", "not authorized",
                                "unauthori", "permission", "invalid token",
                                "session expire", "ab1050", "ab1010")):
            logger.critical("The broker refused the order on AUTHORISATION, not "
                            "on the order itself. The login, quotes and feed can "
                            "all work on a key that has no trading rights — check "
                            "that the Angel app is a TRADING API app and that the "
                            "API key in use belongs to it.")
            return True
        if any(k in d for k in ("margin", "insufficient", "fund")):
            logger.critical("The broker refused the order for MARGIN. Reduce "
                            "risk per trade or max position %, or fund the "
                            "account.")
            return True
        if "rms" in d:
            logger.critical(f"Broker RMS rejection [{label}]: {detail}")
            return True
        return False


    def fetch_order_book(self) -> dict:
        """{orderid: row} in ONE API call, so a verification pass costs one
        request instead of one per order."""
        try:
            api_rate_limiter.wait("orderBook")
            book = config.SMART.orderBook()
            return {str(r.get("orderid")): r
                    for r in (book or {}).get("data", []) or []}
        except Exception as e:
            logger.error(f"orderBook fetch failed: {e}")
            return {}

    def fetch_positions(self) -> dict | None:
        """{tradingsymbol: netqty} from the broker, or None if unreadable.
        Used to prove we are actually flat after the 09:30 square-off."""
        for meth in ("position", "getPosition", "positionData"):
            fn = getattr(config.SMART, meth, None)
            if not callable(fn):
                continue
            try:
                api_rate_limiter.wait("position")
                resp = fn()
                out = {}
                for row in (resp or {}).get("data") or []:
                    sym = str(row.get("tradingsymbol")
                              or row.get("symbolname") or "")
                    try:
                        net = int(float(row.get("netqty", 0) or 0))
                    except (TypeError, ValueError):
                        net = 0
                    if sym:
                        out[sym] = net
                return out
            except Exception as e:
                logger.error(f"positions fetch via {meth} failed: {e}")
                return None
        return None

    def _verify_sl_rests(self, oid, want_trigger, tries=3) -> bool:
        """
        Confirm the order is a LIVE resting stop whose trigger at the exchange
        is the one we intended.

        A returning SDK call is not proof. An order can be accepted and then
        rejected by RMS a moment later, and a modify can silently not take —
        in both cases the position is unprotected while the app believes it
        is covered.
        """
        for _ in range(tries):
            row = self.fetch_order_book().get(str(oid))
            if row:
                status = str(row.get("status", "")).lower()
                if status in ("cancelled", "rejected", "complete", "filled"):
                    return False
                try:
                    got = float(row.get("triggerprice") or 0)
                except (TypeError, ValueError):
                    got = 0.0
                if abs(got - round(float(want_trigger), 2)) < 0.06:
                    return True
            time.sleep(0.5)
        return False

    def _order_status(self, order_id):
        """(status_lower, avg_price, text) for an order id."""
        try:
            api_rate_limiter.wait("orderBook")
            book = config.SMART.orderBook()
            for row in (book or {}).get("data", []) or []:
                if str(row.get("orderid")) == str(order_id):
                    px = row.get("averageprice") or row.get("price") or 0
                    try:
                        px = float(px)
                    except Exception:
                        px = 0.0
                    return (str(row.get("status", "")).lower(), px,
                            str(row.get("text", "")))
        except Exception as e:
            logger.error(f"orderBook lookup failed for {order_id}: {e}")
        return None, 0.0, ""

    def _verify(self, order_id, label):
        """Poll until the order is resolved. Returns (ok, fill_price, detail).

        Inside a fifteen-minute session there is no room for a long timeout,
        so this is deliberately short and loud rather than patient.
        """
        if not order_id:
            return False, 0.0, "no order id returned"
        for _ in range(6):
            status, px, text = self._order_status(order_id)
            if status is None:
                time.sleep(0.8)
                continue
            if status in ("complete", "filled"):
                logger.info(f"FILL CONFIRMED [{label}] id={order_id} @ {px:.2f}")
                return True, px, "complete"
            if status in ("rejected", "cancelled"):
                logger.critical(f"ORDER {status.upper()} [{label}] "
                                f"id={order_id} reason: {text}")
                return False, 0.0, f"{status}: {text}"
            time.sleep(0.8)
        return False, 0.0, "not confirmed within timeout"

    def summary(self):
        return {"realized": round(self.realized, 2),
                "trades": len(self.trades)}
