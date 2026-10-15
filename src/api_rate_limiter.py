"""
api_rate_limiter.py
===================
Enforces Angel One's published throttling limits, per client code.

Three things in Angel's table shape this design and are easy to miss:

  * placeOrder, modifyOrder and cancelOrder are counted TOGETHER. Nine
    requests per second across all three, not nine each. Three separate
    buckets would let us sit inside every individual limit and still be
    refused.

  * Several endpoints have a per-MINUTE cap that binds long before the
    per-second one. details/{id} allows 10/sec but only 500/min — that is
    8.3/sec sustained, so the 10/sec figure is a burst allowance, not a rate
    that can be held.

  * getOrderBook, getPosition and getTradeBook are 1/sec with no minute cap.
    They are the most restricted endpoints Angel offers, and must not carry
    anything that needs frequent polling.

Each bucket therefore carries a minimum interval AND rolling per-minute and
per-hour windows, and wait() blocks until every one of them allows the call.
"""

from __future__ import annotations
import threading
import time
from collections import deque
from typing import Optional

# ----------------------------------------------------------------------
# Angel's published table, transcribed. (per_second, per_minute, per_hour)
# None means not published / not enforced.
# ----------------------------------------------------------------------
LIMITS = {
    "orders":        (9,  500, 1000),   # place + modify + cancel, CUMULATIVE
    "orderDetails":  (10, 500, 5000),   # /order/v1/details/{GuiOrderID}
    "orderBook":     (1,  None, None),
    "position":      (1,  None, None),
    "tradeBook":     (1,  None, None),
    "ltpData":       (10, 500, 5000),
    "quote":         (10, 500, 5000),
    "rmsLimit":      (2,  None, None),
    "getProfile":    (3,  None, 1000),
    "candleData":    (3,  120, 5000),
    "searchScrip":   (1,  None, None),
    "other":         (2,  None, None),
}

# We never spend the whole published allowance.
MARGIN = 0.80


def _normalize_key(api_name: str) -> str:
    n = (api_name or "").lower().replace("_", "").replace("-", "")

    # The cumulative order bucket — these three share one allowance.
    if any(k in n for k in ("placeorder", "modifyorder", "cancelorder")):
        return "orders"
    if n in ("place", "modify", "cancel", "orders"):
        return "orders"

    # Per-order status. Checked BEFORE orderbook so it cannot fall through
    # into the 1/sec bucket.
    if "details" in n or "orderstatus" in n or "individual" in n:
        return "orderDetails"

    if "orderbook" in n:
        return "orderBook"
    if "tradebook" in n:
        return "tradeBook"
    if "position" in n:
        return "position"
    if "ltp" in n:
        return "ltpData"
    if "marketdata" in n or "quote" in n:
        return "quote"
    if "rms" in n or "fund" in n or "margin" in n:
        return "rmsLimit"
    if "profile" in n:
        return "getProfile"
    if "candle" in n or "historical" in n:
        return "candleData"
    if "searchscrip" in n:
        return "searchScrip"
    return "other"


class _Bucket:
    def __init__(self, name, per_second, per_minute, per_hour):
        self.name = name
        self.per_second = per_second
        self.per_minute = per_minute
        self.per_hour = per_hour
        self.min_interval = (1.0 / (per_second * MARGIN)) if per_second else 0.0
        self._base_interval = self.min_interval
        self._last = 0.0
        self._minute = deque()
        self._hour = deque()
        self.calls = 0
        self.waited = 0.0

    @staticmethod
    def _cap(limit):
        return int(limit * MARGIN) if limit else None

    def delay_needed(self, now: float) -> float:
        wait = max(0.0, (self._last + self.min_interval) - now)

        m = self._cap(self.per_minute)
        if m:
            while self._minute and now - self._minute[0] > 60:
                self._minute.popleft()
            if len(self._minute) >= m:
                wait = max(wait, 60 - (now - self._minute[0]) + 0.01)

        h = self._cap(self.per_hour)
        if h:
            while self._hour and now - self._hour[0] > 3600:
                self._hour.popleft()
            if len(self._hour) >= h:
                wait = max(wait, 3600 - (now - self._hour[0]) + 0.01)
        return wait

    def record(self, now: float):
        self._last = now
        self.calls += 1
        if self.per_minute:
            self._minute.append(now)
        if self.per_hour:
            self._hour.append(now)


class ApiRateLimiter:
    def __init__(self, bucket_intervals: Optional[dict] = None):
        self._lock = threading.Lock()
        self._buckets = {name: _Bucket(name, *limits)
                         for name, limits in LIMITS.items()}
        for name, interval in (bucket_intervals or {}).items():
            b = self._buckets.get(_normalize_key(name))
            if b:
                b.min_interval = float(interval)
                b._base_interval = float(interval)

    def wait(self, api_name: str = "other") -> None:
        key = _normalize_key(api_name)
        bucket = self._buckets.get(key) or self._buckets["other"]
        while True:
            with self._lock:
                now = time.monotonic()
                delay = bucket.delay_needed(now)
                if delay <= 0:
                    bucket.record(now)
                    return
            bucket.waited += delay
            time.sleep(min(delay, 1.0))

    def allows_now(self, api_name: str = "other") -> bool:
        """Non-blocking check, for callers that would rather skip than queue."""
        key = _normalize_key(api_name)
        bucket = self._buckets.get(key) or self._buckets["other"]
        with self._lock:
            return bucket.delay_needed(time.monotonic()) <= 0

    def penalise(self, key: str, seconds: float = 2.0):
        bucket = self._buckets.get(_normalize_key(key))
        if bucket is None:
            return
        bucket.min_interval = min(bucket._base_interval * 8,
                                  bucket.min_interval + seconds)

    def relax(self, key: str):
        bucket = self._buckets.get(_normalize_key(key))
        if bucket is not None:
            bucket.min_interval = bucket._base_interval

    def min_delay(self) -> float:
        return float(self._buckets["orderBook"].min_interval)

    def report(self) -> str:
        parts = [f"{n}={b.calls}" for n, b in sorted(self._buckets.items())
                 if b.calls]
        return ", ".join(parts) or "no API calls"

    def log_stats(self) -> None:
        from logger import logger
        logger.info(f"API calls this session: {self.report()}")


api_rate_limiter = ApiRateLimiter()


def wait(api_name: str = "other") -> None:
    api_rate_limiter.wait(api_name)
