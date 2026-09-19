"""
scanner.py
==========
Chooses the ten stocks.

At 09:08:30 the exchange has published an auction price for every stock. We
compare each against yesterday's close, throw out everything that only looks
like a mover, rank what survives, and take the five biggest on each side.

Filter order matters and is taken straight from the strategy note (page 3):
the removals happen BEFORE ranking, so a disqualified name can never push a
genuine mover off the list.

If fewer than five names survive on a side, we trade the shorter list. We
never top it up to reach a round number.
"""

from __future__ import annotations
import csv
import os

import config
import cautionary
from logger import logger

# Reject reasons, in the order they are applied.
R_EXCLUDED = "manual exclusion (ex-div / split)"
R_CAUTION = "under exchange surveillance (API-blocked)"
R_NO_AUCTION = "no auction trades"
R_THIN = "thin auction"
R_PRICE = "price under floor"
R_SMALL = "move under threshold"


def apply_filters(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split the snapshot into (survivors, rejects-with-reason)."""
    s = config.STRATEGY
    excl = config.exclusion_set()
    blocked = cautionary.active()
    keep, drop = [], []

    for r in rows:
        reason = None

        if r["name"].upper() in excl:
            reason = R_EXCLUDED
        elif r["name"].upper() in blocked:
            # Angel will refuse these in the cash segment, so letting one onto
            # the watchlist only burns a slot that a tradeable name could use.
            reason = R_CAUTION
        elif r["auction_price"] <= 0 or r["prev_close"] <= 0 \
                or r["auction_qty"] <= 0:
            reason = R_NO_AUCTION
        elif s["enforce_auction_value"] and \
                r["auction_value"] < float(s["min_auction_value"]):
            reason = R_THIN
        elif r["auction_price"] < float(s["min_price"]):
            reason = R_PRICE
        elif abs(r["gap_pct"]) < float(s["min_gap_pct"]):
            reason = R_SMALL

        if reason:
            drop.append({**r, "reject_reason": reason})
        else:
            keep.append(r)

    counts = {}
    for d in drop:
        counts[d["reject_reason"]] = counts.get(d["reject_reason"], 0) + 1
    logger.info(f"Filters: {len(keep)} of {len(rows)} names survived. "
                f"Removed -> " + ", ".join(f"{k}: {v}"
                                           for k, v in sorted(counts.items())))
    return keep, drop


def rank(survivors: list[dict]) -> list[dict]:
    """Top N gainers and top N losers, tagged with their side."""
    n = int(config.STRATEGY["top_n"])

    gainers = sorted([r for r in survivors if r["gap_pct"] > 0],
                     key=lambda r: r["gap_pct"], reverse=True)[:n]
    losers = sorted([r for r in survivors if r["gap_pct"] < 0],
                    key=lambda r: r["gap_pct"])[:n]

    out = []
    for i, r in enumerate(gainers, 1):
        out.append({**r, "side": "GAINER", "rank": i})
    for i, r in enumerate(losers, 1):
        out.append({**r, "side": "LOSER", "rank": i})

    if len(gainers) < n:
        logger.warning(f"Only {len(gainers)} gainers qualified (wanted {n}). "
                       f"Trading the shorter list — not topping it up.")
    if len(losers) < n:
        logger.warning(f"Only {len(losers)} losers qualified (wanted {n}). "
                       f"Trading the shorter list — not topping it up.")

    logger.info("=" * 62)
    logger.info(f"WATCHLIST LOCKED — {len(out)} names")
    for r in out:
        logger.info(f"  {r['side']:<7} #{r['rank']}  {r['name']:<14} "
                    f"auction {r['auction_price']:>9.2f}  "
                    f"prev {r['prev_close']:>9.2f}  "
                    f"gap {r['gap_pct']:>+7.2f}%  "
                    f"val Rs {r['auction_value']:>12,.0f}  [{r['source']}]")
    logger.info("=" * 62)
    return out


_SCAN_COLS = ["date", "side", "rank", "name", "symbol", "token",
              "auction_price", "prev_close", "gap_pct", "auction_qty",
              "auction_value", "tick_size", "lot_size", "source"]

_REJ_COLS = ["date", "name", "symbol", "auction_price", "prev_close",
             "gap_pct", "auction_qty", "auction_value", "reject_reason"]


def write_scan_csv(watchlist: list[dict], rejects: list[dict]):
    """Stage 1 deliverable: the day's names on disk, plus everything thrown
    out and why. Two weeks of these checked by hand against the exchange is
    what proves the pre-open data is being read correctly."""
    from datetime import datetime
    d = datetime.now().strftime("%Y-%m-%d")

    path = config.scan_file()
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_SCAN_COLS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in watchlist:
            w.writerow({**r, "date": d})
    logger.info(f"Watchlist written to {path}")

    rpath = config.rejects_file()
    new = not os.path.exists(rpath)
    with open(rpath, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_REJ_COLS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rejects:
            w.writerow({**r, "date": d})
    logger.info(f"Reject log written to {rpath} ({len(rejects)} rows)")


def run_scan(snapshot: list[dict]) -> list[dict]:
    """Filter -> rank -> persist. Returns the locked watchlist."""
    survivors, rejects = apply_filters(snapshot)
    watchlist = rank(survivors)
    write_scan_csv(watchlist, rejects)
    return watchlist
