"""
angel_websocket.py
==================
Thin wrapper over Angel One SmartWebSocketV2, subscribing the ten watchlist
stocks in QUOTE mode and routing every tick to the engine:

    on_tick(token, ltp, day_open, high, low, volume, ts)

QUOTE mode is used rather than LTP mode specifically for
`open_price_of_the_day`. That field is the exchange's own opening print,
which is the single reference line this entire strategy is measured
against — reading it from the feed rather than inferring it from the first
tick removes the one assumption that would silently corrupt every signal.

Connection pattern follows the reference Angel One WebSocket code, with
auto-reconnect and resubscription.
"""

from __future__ import annotations
import threading
import time
from datetime import datetime

import config
from logger import logger

try:
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
except Exception:
    SmartWebSocketV2 = None


WS_EXCHANGE_TYPE = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5}
MODE_QUOTE = 2  # 1=LTP, 2=Quote, 3=SnapQuote


class WebSocketFeed:
    def __init__(self, on_tick):
        self.on_tick = on_tick
        self.sws = None
        self.connected = False
        self.tokens_by_exch = {}
        self.tick_count = 0
        self.last_tick_ts = None
        self._reconnects = 0
        self._max_reconnects = 10
        self._stop = False

    # ------------------------------------------------------------------
    def add_token(self, token: str, exchange: str = "NSE"):
        ex = WS_EXCHANGE_TYPE.get(exchange, 1)
        self.tokens_by_exch.setdefault(ex, [])
        if str(token) not in self.tokens_by_exch[ex]:
            self.tokens_by_exch[ex].append(str(token))

    def _sub_list(self):
        # A COPY of each token list, not the list itself. The SDK keeps the
        # object it is handed for its own resubscribe bookkeeping and appends
        # to it on reconnect — passing the live list is why the subscribed
        # count doubled on every post-session reconnect.
        return [{"exchangeType": ex, "tokens": list(toks)}
                for ex, toks in self.tokens_by_exch.items() if toks]

    def token_count(self):
        return sum(len(t) for t in self.tokens_by_exch.values())

    # ------------------------------------------------------------------
    def start(self):
        if SmartWebSocketV2 is None:
            logger.error("SmartWebSocketV2 not available (SDK missing).")
            return
        threading.Thread(target=self._connect, daemon=True).start()

    def stop(self):
        self._stop = True
        try:
            if self.sws:
                self.sws.close_connection()
        except Exception:
            pass

    def resubscribe(self):
        """Push the current token set to an already-open socket. Used when
        the option/futures leg is resolved after the initial subscribe."""
        if not (self.connected and self.sws):
            return
        try:
            self.sws.subscribe("pomr_feed", MODE_QUOTE, self._sub_list())
            logger.info(f"Re-subscribed: {self.token_count()} tokens.")
        except Exception as e:
            logger.error(f"Re-subscribe error: {e}")

    # ------------------------------------------------------------------
    def _connect(self):
        if self._stop:
            return
        try:
            auth = getattr(config, "AUTH_TOKEN", None)
            feed = getattr(config, "FEED_TOKEN", None)
            if not auth or not feed:
                logger.error("WebSocket: missing auth/feed token.")
                return
            self.sws = SmartWebSocketV2(
                auth, config.CREDENTIALS["api_key"],
                config.CREDENTIALS["client_id"], feed)
            self.sws.on_open = self._on_open
            self.sws.on_data = self._on_data
            self.sws.on_error = self._on_error
            self.sws.on_close = self._on_close
            logger.info("Connecting WebSocket feed...")
            self.sws.connect()
        except Exception as e:
            logger.error(f"WebSocket connect error: {e}")
            self._reconnect()

    def _on_open(self, wsapp):
        self.connected = True
        self._reconnects = 0
        logger.info("WebSocket feed connected; subscribing watchlist...")
        try:
            self.sws.subscribe("pomr_feed", MODE_QUOTE, self._sub_list())
            logger.info(f"Subscribed {self.token_count()} tokens in QUOTE mode.")
        except Exception as e:
            logger.error(f"Subscribe error: {e}")

    def _on_data(self, wsapp, message):
        try:
            token = message.get("token")
            ltp = message.get("last_traded_price")
            if token is None or ltp is None:
                return
            # NSE/NFO prices arrive in paise.
            px = float(ltp) / 100.0
            day_open = float(message.get("open_price_of_the_day", 0) or 0) / 100.0
            high = float(message.get("high_price_of_the_day", 0) or 0) / 100.0
            low = float(message.get("low_price_of_the_day", 0) or 0) / 100.0
            vol = float(message.get("volume_trade_for_the_day", 0) or 0)
            ts = datetime.now()
            self.tick_count += 1
            self.last_tick_ts = ts
            self.on_tick(str(token), px, day_open, high, low, vol, ts)
        except Exception as e:
            logger.error(f"WS data error: {e}")

    def _on_error(self, wsapp, error):
        logger.error(f"WebSocket error: {error}")
        self.connected = False

    def _on_close(self, wsapp):
        self.connected = False
        if not self._stop:
            logger.warning("WebSocket closed; reconnecting...")
            self._reconnect()

    def _reconnect(self):
        if self._stop or self._reconnects >= self._max_reconnects:
            if self._reconnects >= self._max_reconnects:
                logger.critical("WebSocket gave up reconnecting. The feed is "
                                "DOWN — check open positions manually.")
            return
        self._reconnects += 1
        # Inside a fifteen-minute session a long backoff is the same as no
        # feed at all, so cap it hard.
        wait = min(8, self._reconnects * 2)
        logger.warning(f"WS reconnect attempt {self._reconnects} in {wait}s")
        time.sleep(wait)
        if self._stop:
            return
        threading.Thread(target=self._connect, daemon=True).start()
