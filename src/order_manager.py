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

_ORDER_COLS = ["time", "mode", "name", "instrument", "symbol", "side",
               "qty", "price", "order_type", "order_id", "status", "detail"]

_TRADE_COLS = ["date", "name", "side", "instrument", "symbol", "gap_pct",
               "open_price", "test_extreme", "test_depth_pct",
               "entry_time", "entry_price", "stop_price", "qty",
               "risk_amount", "exit_time", "exit_price", "exit_reason",
               "pnl", "pnl_pct_of_capital", "hold_seconds", "note"]


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
                         f"ENTRY {side} {qty} {inst['symbol']}")
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
        oid = self._send(params, f"STOP {side} {qty} {inst['symbol']} @ {trig}")
        self._log_order(st.name, inst, side, qty, trig, "STOPLOSS_LIMIT",
                        order_id=oid or "",
                        status="RESTING" if oid else "FAILED")
        if oid:
            self.sl_orders[st.name] = oid
            logger.info(f"{st.name}: protective stop resting at {trig:.2f} "
                        f"(limit {limit:.2f}), id={oid}")
        else:
            logger.critical(f"{st.name}: PROTECTIVE STOP NOT PLACED. The "
                            f"position is unprotected at the broker — the "
                            f"engine stop is the only cover.")
        return oid

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
             reason: str) -> tuple[float, str]:
        """
        Close the position. Returns (fill_price, resolved_reason).

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
                return fill_px, "STOP_HIT"
            self.cancel_stop(st.name)

            oid = self._send(self._market_params(inst, qty, side),
                             f"EXIT {side} {qty} {inst['symbol']} ({reason})")
            self._log_order(st.name, inst, side, qty, ref_price, "MARKET",
                            order_id=oid or "", status="SUBMITTED",
                            detail=reason)
            ok, fill, detail = self._verify(oid, f"EXIT {inst['symbol']}")
            self._log_order(st.name, inst, side, qty, fill or ref_price,
                            "MARKET", order_id=oid or "",
                            status="COMPLETE" if ok else "FAILED",
                            detail=f"{reason}; {detail}")
            if not ok:
                logger.critical(f"!!! EXIT MAY HAVE FAILED for {st.name} "
                                f"({reason}). CHECK THE TERMINAL — the "
                                f"position may still be OPEN. !!!")
            return (fill or ref_price), reason

        px = self._paper_fill(ref_price, side)
        self._log_order(st.name, inst, side, qty, px, "MARKET",
                        status="PAPER_FILL", detail=reason)
        logger.info(f"[PAPER] {side} {qty} {inst['symbol']} @ {px:.2f} ({reason})")
        return px, reason

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

    def _send(self, params, label):
        for attempt in range(config.RETRY["max_retries"]):
            try:
                api_rate_limiter.wait("placeOrder")
                oid = config.SMART.placeOrder(params)
                if oid and len(str(oid)) > 4:
                    logger.info(f"Order submitted [{label}] id={oid}")
                    return str(oid)
                logger.error(f"Order returned bad id [{label}]: {oid}")
            except Exception as e:
                msg = str(e)
                logger.error(f"Order error [{label}] attempt {attempt+1}: {msg}")
                if "short" in msg.lower() and "not allow" in msg.lower():
                    logger.critical("Broker rejected the short on this scrip. "
                                    "Switch short_instrument to FUTURES for "
                                    "this name.")
                    return None
            time.sleep([1, 2, 4][min(attempt, 2)])
        logger.critical(f"Order FAILED after retries [{label}]")
        return None

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
