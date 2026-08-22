"""
strategy.py
===========
The state machine for one stock, for one morning.

Everything is measured against a single price: where the stock opened at
09:15. Never yesterday's close, never a moving average, never an indicator.
Price crossing that line is the signal.

    WAITING_OPEN   the 09:15 opening print has not arrived yet
    WAITING_TEST   line established; the stock has not been through it
                   against the direction of its own gap
    TESTED         it has been through, clearly; the extreme reached out
                   there is the stop, and it keeps updating while the stock
                   stays out there
    ENTERED        it came back through and we are in
    CLOSED         stopped out, or squared off at 09:30
    DISQUALIFIED   the test ran deeper than the guard rail allows
    MISSED         09:28 passed with the pattern incomplete, or 09:30 came

The early check is not a separate branch. Ticks are fed through the same
machine from 09:15:00, but no order may be placed before the check moment.
A stock that completed its out-and-back inside the first thirty seconds is
therefore standing at an entry condition the instant the check arrives, and
fires immediately — which is exactly what the early check describes.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime

import config
from logger import logger

# ---- states ----
WAITING_OPEN = "WAITING_OPEN"
WAITING_TEST = "WAITING_TEST"
TESTED = "TESTED"
ENTERED = "ENTERED"
CLOSED = "CLOSED"
DISQUALIFIED = "DISQUALIFIED"
MISSED = "MISSED"
SKIPPED = "SKIPPED"

LIVE_STATES = (WAITING_OPEN, WAITING_TEST, TESTED)


@dataclass
class StockState:
    # ---- identity, from the morning scan ----
    name: str
    symbol: str
    token: str
    side: str                      # GAINER | LOSER
    rank: int
    tick_size: float
    lot_size: int
    auction_price: float
    prev_close: float
    gap_pct: float

    # ---- the line ----
    open_price: float = 0.0
    open_source: str = ""

    # ---- machine ----
    state: str = WAITING_OPEN
    beyond_line: bool = False      # currently on the wrong side of the line
    test_extreme: float | None = None   # low for a gainer, high for a loser
    test_time: datetime | None = None
    ltp: float = 0.0
    last_tick: datetime | None = None
    tick_count: int = 0

    # ---- the trade ----
    entry_price: float = 0.0
    entry_time: datetime | None = None
    stop_price: float = 0.0
    initial_stop: float = 0.0
    qty: int = 0
    risk_amount: float = 0.0
    instrument: dict = field(default_factory=dict)   # what we actually traded
    exit_price: float = 0.0
    exit_time: datetime | None = None
    exit_reason: str = ""
    pnl: float = 0.0
    note: str = ""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @property
    def is_long(self) -> bool:
        return self.side == "GAINER"

    @property
    def buffer(self) -> float:
        """How far past the line counts as 'clearly through'.

        Prices in the opening minutes jitter by a rupee or two constantly.
        Without this, a single print one paisa the wrong side of the opening
        price would arm almost every stock within seconds.
        """
        s = config.STRATEGY
        base = self.open_price or self.auction_price
        pct = float(s["cross_buffer_pct"]) / 100.0 * base
        ticks = float(s["min_cross_ticks"]) * self.tick_size
        return max(pct, ticks)

    def test_depth_pct(self) -> float:
        if not (self.open_price and self.test_extreme):
            return 0.0
        return abs(self.test_extreme - self.open_price) / self.open_price * 100.0

    # ------------------------------------------------------------------
    def set_open_price(self, px: float, source: str):
        if self.open_price or px <= 0:
            return
        self.open_price = round(float(px), 2)
        self.open_source = source
        self.state = WAITING_TEST

        drift = 0.0
        if self.auction_price:
            drift = abs(self.open_price - self.auction_price) / self.auction_price * 100.0
        msg = (f"{self.name}: line set at {self.open_price:.2f} "
               f"[{source}]  (auction {self.auction_price:.2f}, "
               f"buffer {self.buffer:.2f})")
        if drift > 0.5:
            logger.warning(msg + f"  — DIFFERS from the auction price by "
                                 f"{drift:.2f}%, verify against the terminal")
        else:
            logger.info(msg)

    # ------------------------------------------------------------------
    def on_tick(self, px: float, ts: datetime) -> str | None:
        """
        Advance the machine one tick.

        Returns 'ENTER' when the reclaim has happened and the stock is
        standing at an entry condition. The engine decides whether it is
        allowed to act (check moment reached, limits not hit) — this
        function never places anything.
        """
        if px <= 0:
            return None
        self.ltp = px
        self.last_tick = ts
        self.tick_count += 1

        if self.state not in (WAITING_TEST, TESTED):
            return None
        if not self.open_price:
            return None

        buf = self.buffer
        line = self.open_price

        if self.is_long:
            out_there = px <= line - buf     # gainer trading clearly below
            reclaimed = px >= line + buf     # ...and clearly back above
        else:
            out_there = px >= line + buf     # loser trading clearly above
            reclaimed = px <= line - buf     # ...and clearly back below

        # ---- the test: through the line, against the gap ----
        if out_there:
            if not self.beyond_line:
                self.beyond_line = True
                if self.state == WAITING_TEST:
                    self.state = TESTED
                    self.test_time = ts
                    logger.info(f"{self.name}: TEST — trading "
                                f"{'below' if self.is_long else 'above'} the "
                                f"line at {px:.2f}")
            # the extreme keeps updating for as long as it stays out there
            if self.test_extreme is None:
                self.test_extreme = px
            elif self.is_long:
                self.test_extreme = min(self.test_extreme, px)
            else:
                self.test_extreme = max(self.test_extreme, px)

            # guard rail: a stock swinging this hard is not showing our
            # pattern, and the stop it implies is too wide to size against
            depth = self.test_depth_pct()
            if depth > float(config.STRATEGY["max_test_pct"]):
                self.state = DISQUALIFIED
                self.note = (f"test ran {depth:.2f}% through the line "
                             f"(cap {config.STRATEGY['max_test_pct']}%)")
                logger.warning(f"{self.name}: DISQUALIFIED — {self.note}")
            return None

        # ---- back inside the line ----
        if self.beyond_line and not out_there:
            self.beyond_line = False

        # ---- the entry: clearly back through, in the gap direction ----
        if self.state == TESTED and reclaimed and self.test_extreme is not None:
            return "ENTER"

        return None

    # ------------------------------------------------------------------
    def build_stop(self) -> float:
        """
        The stop sits just the other side of the extreme made during the
        test. If price returns there, the reason we took the trade has been
        disproved.
        """
        tick = self.tick_size or 0.05
        if self.is_long:
            return round(self.test_extreme - tick, 2)
        return round(self.test_extreme + tick, 2)

    def size(self, entry: float, stop: float, capital: float) -> tuple[int, float, float, str]:
        """
        Position size is not chosen in advance — it falls out of where the
        stop sits. A tight stop means more shares, a wide stop fewer, and
        the amount at risk stays the same either way.

        Returns (qty, stop_used, risk_taken, note).
        """
        s = config.STRATEGY
        risk_budget = capital * float(s["risk_pct"]) / 100.0
        note = ""

        # guard rail 1: a very shallow test would produce an enormous share
        # count, so widen the stop to a minimum distance instead
        min_dist = float(s["min_stop_pct"]) / 100.0 * self.open_price
        dist = abs(entry - stop)
        if dist < min_dist:
            stop = round(entry - min_dist, 2) if self.is_long \
                else round(entry + min_dist, 2)
            note = f"stop widened to the {s['min_stop_pct']}% floor"
            dist = abs(entry - stop)

        if dist <= 0:
            return 0, stop, 0.0, "zero stop distance"

        qty = int(risk_budget // dist)

        # guard rail 3: no position takes more than a third of the account
        max_value = capital * float(s["max_position_pct"]) / 100.0
        cap_qty = int(max_value // entry) if entry > 0 else 0
        if cap_qty < qty:
            qty = cap_qty
            note = (note + "; " if note else "") + \
                   f"size capped at {s['max_position_pct']}% of account"

        risk_taken = qty * dist
        return qty, stop, risk_taken, note

    # ------------------------------------------------------------------
    def unrealized(self) -> float:
        if self.state != ENTERED or not self.qty or not self.ltp:
            return 0.0
        if self.is_long:
            return (self.ltp - self.entry_price) * self.qty
        return (self.entry_price - self.ltp) * self.qty

    def stop_breached(self, px: float) -> bool:
        if self.state != ENTERED or not self.stop_price:
            return False
        return px <= self.stop_price if self.is_long else px >= self.stop_price

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "side": "LONG" if self.is_long else "SHORT",
            "state": self.state,
            "gap_pct": self.gap_pct,
            "open": self.open_price,
            "extreme": self.test_extreme or 0.0,
            "ltp": self.ltp,
            "entry": self.entry_price,
            "stop": self.stop_price,
            "qty": self.qty,
            "pnl": self.pnl if self.state == CLOSED else self.unrealized(),
            "note": self.note,
        }
