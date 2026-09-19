"""
cautionary.py
=============
Angel One blocks equity scrips under exchange surveillance measures (ASM /
ESM / GSM) from the placeOrder API — since 24 January 2025, and only for the
equity segment. F&O and commodities are unaffected. Such a scrip can only be
traded on Angel's own app or website, where the cautionary message is shown
and consent given; there is no API equivalent, so the order simply fails with

    AB4036  "The order cannot be processed as the token is categorised
             under cautionary listings by the exchange."

There is no published endpoint to ask whether a token is listed, so this
module learns it the only way available: the first time a name is refused
with AB4036 it is recorded, and from then on the scanner drops it before it
can take a slot on the watchlist.

Entries age out, because surveillance status is reviewed periodically and a
name that leaves the list should come back into the universe on its own.
"""

from __future__ import annotations
import json
import os
from datetime import datetime, timedelta

import config
from logger import logger

FILE = os.path.join(config.DATA_DIR, "cautionary.json")
TTL_DAYS = 30


def _load() -> dict:
    try:
        if os.path.exists(FILE):
            with open(FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception as e:
        logger.error(f"Could not read the cautionary list: {e}")
    return {}


def _save(data: dict):
    try:
        with open(FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except Exception as e:
        logger.error(f"Could not write the cautionary list: {e}")


def active() -> set:
    """Names still considered blocked, after ageing out stale entries."""
    data = _load()
    cutoff = datetime.now() - timedelta(days=TTL_DAYS)
    live, expired = {}, []
    for name, seen in data.items():
        try:
            when = datetime.fromisoformat(str(seen))
        except Exception:
            when = datetime.now()
        if when >= cutoff:
            live[name] = seen
        else:
            expired.append(name)
    if expired:
        _save(live)
        logger.info(f"Cautionary list: {len(expired)} name(s) aged out and are "
                    f"tradeable again — {', '.join(sorted(expired))}")
    return set(live.keys())


def record(name: str):
    """Mark a name as refused by the broker under AB4036."""
    if not name:
        return
    name = name.upper()
    data = _load()
    first = name not in data
    data[name] = datetime.now().isoformat(timespec="seconds")
    _save(data)
    if first:
        logger.critical(
            f"{name} is under exchange surveillance and CANNOT be traded "
            f"through the Angel API in the cash segment. It has been added to "
            f"the cautionary list and will be dropped from the watchlist for "
            f"the next {TTL_DAYS} days. Trading it would need Angel's own app "
            f"or web terminal, or the F&O route.")
    else:
        logger.warning(f"{name}: refused again under AB4036 (already on the "
                       f"cautionary list).")


def forget(name: str):
    """Manual override — take a name off the list."""
    data = _load()
    if data.pop(str(name).upper(), None) is not None:
        _save(data)
        logger.info(f"{name} removed from the cautionary list.")
