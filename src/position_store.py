"""
position_store.py
=================
Survives a restart.

Every failure mode we have chased so far has been the app drawing a wrong
conclusion from the broker. This file covers the one case where the app
draws no conclusion at all because it is not running: a crash, a reboot, or
the operator pressing Stop mid-session — which happened on 1 October at
09:22, leaving two positions open that nothing squared off.

Open positions are written to disk the moment they change. On startup the
file is read back and checked against the broker's own position book, so a
restarted app adopts what it was managing instead of forgetting it.
"""

from __future__ import annotations
import json
import os
from datetime import datetime

import config
from logger import logger

FILE = os.path.join(config.DATA_DIR, "open_positions.json")


def save(states: dict):
    """Write every position the app currently believes is open."""
    try:
        rows = []
        for st in states.values():
            if st.state not in ("ENTERED", "UNCONFIRMED"):
                continue
            rows.append({
                "name": st.name, "symbol": st.symbol, "token": st.token,
                "side": st.side, "qty": st.qty,
                "entry_price": st.entry_price, "stop_price": st.stop_price,
                "initial_stop": st.initial_stop, "open_price": st.open_price,
                "tick_size": st.tick_size, "state": st.state,
                "instrument": st.instrument,
                "trail_steps": st.trail_steps,
                "breakeven_done": st.breakeven_done,
            })
        payload = {"date": datetime.now().strftime("%Y-%m-%d"),
                   "written": datetime.now().isoformat(timespec="seconds"),
                   "positions": rows}
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, FILE)          # atomic: never a half-written file
    except Exception as e:
        logger.error(f"Could not write the position store: {e}")


def load() -> list:
    """Positions recorded by a previous run TODAY. Yesterday's are ignored —
    intraday positions do not survive the night."""
    try:
        if not os.path.exists(FILE):
            return []
        with open(FILE, "r", encoding="utf-8") as f:
            payload = json.load(f) or {}
        if payload.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return []
        return payload.get("positions") or []
    except Exception as e:
        logger.error(f"Could not read the position store: {e}")
        return []


def clear():
    try:
        if os.path.exists(FILE):
            os.remove(FILE)
    except Exception:
        pass
