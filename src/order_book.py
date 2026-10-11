"""
order_book.py
=============
ONE reader of the Angel order book, for the whole application.

The problem this solves: every place that wanted to confirm something used to
issue its own getOrderBook calls. An entry verified itself (up to six reads),
its protective stop verified itself (three more), the monitor polled, the
reconciler polled. Four entries inside forty seconds put roughly forty-five
reads on an endpoint Angel allows about one per second — which is why the
broker started refusing and then timing out, and why verification was least
reliable at exactly the moment it mattered most.

The fix is not to verify less. It is to verify from a single shared snapshot.
This thread reads the book on a fixed cadence and publishes the result;
everyone else waits for a snapshot newer than the moment they care about and
reads it. The API cost is therefore constant — one read per interval — no
matter how many orders are in flight.

A snapshot carries the time it was captured, so a caller can insist on
evidence that post-dates its own action ("show me the book as it was AFTER I
sent that order"), which is what makes it proof rather than a stale guess.
"""

from __future__ import annotations
import threading
import time
from datetime import datetime

import config
from logger import logger
from api_rate_limiter import api_rate_limiter


class OrderBookWatcher:
    def __init__(self, interval: float = 1.5):
        self.interval = float(interval)
        self._book: dict = {}
        self._at: float = 0.0          # monotonic time of the last good read
        self._ok = False               # was the most recent read successful
        self._seq = 0
        self._calls = 0
        self._failures = 0
        self._consecutive_failures = 0
        self._lock = threading.Condition()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------
    def start(self):
        if self._thread or config.TRADING_MODE != "LIVE":
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="pomr-orderbook")
        self._thread.start()
        logger.info(f"Order book watcher started — one read every "
                    f"{self.interval:.1f}s ({1 / self.interval:.2f}/sec).")

    def stop(self):
        self._stop.set()
        with self._lock:
            self._lock.notify_all()

    # ------------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            self._read_once()
            elapsed = time.monotonic() - started
            # Back off while the broker is unhappy, then return to cadence.
            wait = self.interval - elapsed
            if self._consecutive_failures:
                wait = max(wait, min(8.0, self.interval * (1 + self._consecutive_failures)))
            if wait > 0:
                self._stop.wait(wait)

    def _read_once(self):
        try:
            api_rate_limiter.wait("orderBook")
            self._calls += 1
            resp = config.SMART.orderBook()
            book = {str(r.get("orderid")): r
                    for r in (resp or {}).get("data", []) or []}
            with self._lock:
                self._book = book
                self._at = time.monotonic()
                self._ok = True
                self._seq += 1
                self._consecutive_failures = 0
                self._lock.notify_all()
        except Exception as e:
            self._failures += 1
            self._consecutive_failures += 1
            with self._lock:
                self._ok = False
                self._lock.notify_all()
            if self._consecutive_failures in (1, 4, 12):
                logger.warning(f"Order book read failed "
                               f"({self._consecutive_failures} in a row): "
                               f"{str(e)[:120]}")

    # ------------------------------------------------------------------
    def current(self) -> tuple[dict, float, bool]:
        """(book, age_seconds, is_current). Never blocks."""
        with self._lock:
            if not self._at:
                return {}, float("inf"), False
            return dict(self._book), time.monotonic() - self._at, self._ok

    def fresh_after(self, marker: float, timeout: float = 8.0) -> dict | None:
        """
        Wait for a snapshot captured AFTER `marker` (a time.monotonic() value
        taken before the action you want evidence about).

        Returns the book, or None if no fresh snapshot arrived in time — which
        means "we do not know", never "it is not there".
        """
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._at <= marker:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    return None
                self._lock.wait(min(remaining, 0.5))
            return dict(self._book)

    @staticmethod
    def marker() -> float:
        """Take this immediately BEFORE sending an order."""
        return time.monotonic()

    def stats(self) -> dict:
        with self._lock:
            age = (time.monotonic() - self._at) if self._at else None
        return {"calls": self._calls, "failures": self._failures,
                "age": age, "healthy": self._consecutive_failures == 0}

    def report(self, session_seconds: float):
        s = self.stats()
        rate = s["calls"] / session_seconds if session_seconds > 0 else 0
        logger.info(f"Order book: {s['calls']} reads in "
                    f"{int(session_seconds)}s ({rate:.2f}/sec), "
                    f"{s['failures']} failed.")


watcher = OrderBookWatcher()
