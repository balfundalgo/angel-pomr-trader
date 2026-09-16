"""
config.py
=========
Central configuration + runtime state for the Pre-Open Momentum Reclaim
(POMR) strategy — "The First Fifteen Minutes", Balfund Strategy Note 01 v1.3.

Credentials are NOT hardcoded. They are entered in the GUI at runtime and
pushed into CREDENTIALS here, then persisted to settings.json next to the EXE.
"""

import os
import sys
import json
from datetime import datetime, timedelta

# ----------------------------------------------------------------------
# Angel One credentials (filled at runtime by the GUI)
# ----------------------------------------------------------------------
CREDENTIALS = {
    "client_id": "",
    "api_key": "",
    "mpin": "",
    "totp_secret": "",
}

# ----------------------------------------------------------------------
# Run mode
#   SCAN_ONLY : rank the F&O list, write the day's names to CSV, no trading.
#               This is Stage 1 of the build plan in the strategy note.
#   PAPER     : full live decisions on the live feed, simulated fills.
#   LIVE      : real orders.
# ----------------------------------------------------------------------
TRADING_MODE = "SCAN_ONLY"

APP_VERSION = "1.0.0"
APP_NAME = "Balfund POMR — The First Fifteen Minutes"

# ----------------------------------------------------------------------
# Strategy parameters (all overridable from the GUI)
# Defaults are the "start with" column of the strategy note, page 10.
# ----------------------------------------------------------------------
STRATEGY = {
    # ---- the morning timetable (IST, machine clock) ----
    "scan_time":        "09:08:30",   # rank the F&O list, lock the ten names
    "feed_time":        "09:14:00",   # connect + verify the live feed
    "open_time":        "09:15:00",   # market opens, record each opening price
    "check_seconds":    30,           # warm-up delay after the open; 0 = none
    "last_entry_time":  "09:28:00",   # after this, setups are abandoned
    "square_off_time":  "09:30:00",   # everything closed, no exceptions

    # ---- choosing the ten stocks ----
    "top_n":            5,            # per side (gainers / losers)
    "min_gap_pct":      0.75,         # moves smaller than this are noise
    "min_price":        50.0,         # stocks under Rs 50 are dropped
    "min_auction_value": 500000.0,    # thin-auction cut (Rs of matched value)
    "enforce_auction_value": True,    # turn off while calibrating in Stage 1
    "exclusions":       "",           # comma-separated: ex-div / split names

    # ---- "clearly through" the line ----
    "cross_buffer_pct": 0.05,         # % of the opening price
    "min_cross_ticks":  2,            # ...but never fewer than N ticks

    # ---- size, stop, risk ----
    "capital":          1000000.0,    # used when use_live_balance is off
    "use_live_balance": True,         # read the RMS balance each morning
    "risk_pct":         1.0,          # 1% of the account per trade
    "min_stop_pct":     0.25,         # guard rail: stop too tight -> widen
    "max_test_pct":     1.50,         # guard rail: test too deep -> skip
    "max_position_pct": 33.33,        # guard rail: no position over a third

    # ---- trailing stop (off by default) ----
    # A three-stage ratchet, all three measured as a percentage of the
    # ENTRY price so the behaviour is identical on a Rs 670 stock and a
    # Rs 9,200 one:
    #   1. profit reaches trail_trigger_pct  -> stop moves to the entry price
    #   2. every further trail_step_pct      -> stop moves trail_move_pct more
    #   3. the stop only ever tightens; a retrace never gives a step back
    # The stop is also held at least min_stop_pct behind the current price,
    # so a move larger than the step can never walk the stop into the market.
    "trail_enabled":    False,
    "trail_trigger_pct": 0.50,        # X — breakeven trigger
    "trail_step_pct":   0.25,         # Y — each further step of profit
    "trail_move_pct":   0.25,         # Z — how far the stop moves per step

    # ---- daily limits ----
    "max_trades":       4,            # total entries for the session
    "max_positions":    4,            # open at any one moment
    "daily_loss_cap_pct": 3.0,        # closes everything and stops

    # ---- instrument ----
    "instrument":       "STOCK",      # STOCK | OPTION
    "short_instrument": "CASH",       # CASH | FUTURES  (fallback for shorts)
    "option_max_spread_pct": 1.5,     # bid/ask spread as % of mid
    "option_min_oi":    500,          # contracts
    "option_require_traded": True,    # must have traded today
    "option_delta_est": 0.5,          # ATM delta assumed when sizing lots

    # ---- plumbing ----
    "preopen_source":   "ANGEL_THEN_NSE",  # ANGEL | NSE | ANGEL_THEN_NSE
    "place_exchange_sl": True,        # resting SL with the broker
    "paper_slippage_pct": 0.05,       # each side, PAPER mode only
}

RETRY = {"max_retries": 3, "retry_delay": 1}

# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------
def _base_dir():
    """Writable base dir; works for a frozen EXE and for source runs."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _base_dir()
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

SCRIP_MASTER_FILE = os.path.join(DATA_DIR, "scrip_master.json")
SCRIP_MASTER_URL = ("https://margincalculator.angelbroking.com/"
                    "OpenAPI_File/files/OpenAPIScripMaster.json")

SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")


def _stamp():
    return datetime.now().strftime("%Y%m%d")


def log_file():
    return os.path.join(LOG_DIR, f"pomr_activity_{_stamp()}.log")


def trades_file():
    """One row per completed round trip."""
    return os.path.join(LOG_DIR, f"pomr_trades_{_stamp()}.csv")


def orders_file():
    """One row per order event (submission / fill / rejection)."""
    return os.path.join(LOG_DIR, f"pomr_orders_{_stamp()}.csv")


def scan_file():
    """The morning watchlist — Stage 1 deliverable."""
    return os.path.join(LOG_DIR, f"pomr_scan_{_stamp()}.csv")


def rejects_file():
    """Everything the filters threw out, with the reason."""
    return os.path.join(LOG_DIR, f"pomr_rejects_{_stamp()}.csv")


# ----------------------------------------------------------------------
# Settings persistence
# ----------------------------------------------------------------------
def save_settings() -> bool:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"credentials": CREDENTIALS,
                       "trading_mode": TRADING_MODE,
                       "strategy": STRATEGY}, f, indent=2)
        return True
    except Exception:
        return False


def load_settings() -> bool:
    global TRADING_MODE
    if not os.path.exists(SETTINGS_FILE):
        return False
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        CREDENTIALS.update(data.get("credentials", {}) or {})
        TRADING_MODE = data.get("trading_mode", TRADING_MODE)
        STRATEGY.update(data.get("strategy", {}) or {})
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------
# Time helpers (everything in this strategy is clock-driven)
# ----------------------------------------------------------------------
def today_at(hhmmss: str) -> datetime:
    """'09:15:00' or '09:15' -> a datetime on today's date."""
    parts = [int(p) for p in str(hhmmss).strip().split(":")]
    while len(parts) < 3:
        parts.append(0)
    now = datetime.now()
    return now.replace(hour=parts[0], minute=parts[1], second=parts[2],
                       microsecond=0)


def open_dt() -> datetime:
    return today_at(STRATEGY["open_time"])


def check_dt() -> datetime:
    return open_dt() + timedelta(seconds=int(STRATEGY["check_seconds"]))


def last_entry_dt() -> datetime:
    return today_at(STRATEGY["last_entry_time"])


def square_off_dt() -> datetime:
    return today_at(STRATEGY["square_off_time"])


def exclusion_set() -> set:
    raw = str(STRATEGY.get("exclusions", "") or "")
    return {s.strip().upper() for s in raw.replace(";", ",").split(",")
            if s.strip()}


# ----------------------------------------------------------------------
# Runtime objects (set during a session)
# ----------------------------------------------------------------------
SMART = None        # SmartConnect object
AUTH_TOKEN = None   # jwt token   (WebSocket auth)
FEED_TOKEN = None   # feed token  (WebSocket auth)
