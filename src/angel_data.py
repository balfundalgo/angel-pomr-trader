"""
angel_data.py
=============
Market-data layer over Angel One SmartAPI for the POMR strategy:

  * download / cache the scrip master
  * build the F&O STOCK universe (FUTSTK underlyings mapped to their NSE
    cash tokens)
  * take the pre-open snapshot at 09:08:30 — the auction equilibrium price
    and yesterday's close for every name in the universe
  * resolve the ATM option (current-month) and the near-month future for a
    given underlying, for the alternate instrument routes
  * read the account balance so risk is sized off the live figure

The pre-open snapshot is the one piece of this strategy that has no
guaranteed broker endpoint. Angel disseminates the equilibrium price on the
normal quote path once the auction closes, which is what we read first. An
optional NSE public-endpoint cross-check is available as a fallback because
if this number is wrong, every trade after it is wrong too (strategy note,
page 12).
"""

from __future__ import annotations
import os
import json
import time
from datetime import datetime

import pandas as pd
import requests

import config
from logger import logger
from api_rate_limiter import api_rate_limiter

# Angel caps getMarketData at 50 tokens per request.
QUOTE_CHUNK = 50

_NSE_PREOPEN_URL = "https://www.nseindia.com/api/market-data-pre-open?key=FO"
_NSE_HOME = "https://www.nseindia.com"
_NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


# ======================================================================
# Scrip master
# ======================================================================
def download_scrip_master(force: bool = False) -> bool:
    """Download the public Angel scrip master JSON (no auth needed)."""
    try:
        if (not force) and os.path.exists(config.SCRIP_MASTER_FILE):
            age = time.time() - os.path.getmtime(config.SCRIP_MASTER_FILE)
            if age < 12 * 3600:
                logger.info("Scrip master is fresh; reusing cached copy.")
                return True
        logger.info("Downloading Angel scrip master...")
        r = requests.get(config.SCRIP_MASTER_URL, timeout=90)
        r.raise_for_status()
        with open(config.SCRIP_MASTER_FILE, "w", encoding="utf-8") as f:
            f.write(r.text)
        logger.info("Scrip master saved.")
        return True
    except Exception as e:
        logger.error(f"Scrip master download failed: {e}")
        if os.path.exists(config.SCRIP_MASTER_FILE):
            logger.warning("Falling back to the cached scrip master.")
            return True
        return False


_MASTER_CACHE = None


def _master() -> pd.DataFrame:
    global _MASTER_CACHE
    if _MASTER_CACHE is not None:
        return _MASTER_CACHE
    with open(config.SCRIP_MASTER_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    df = pd.DataFrame(data)
    for c in ("symbol", "name", "exch_seg", "instrumenttype", "token",
              "expiry", "strike", "lotsize", "tick_size"):
        if c not in df.columns:
            df[c] = ""
    _MASTER_CACHE = df
    return df


# ======================================================================
# The F&O stock universe
# ======================================================================
def fo_stock_universe() -> list[dict]:
    """
    Every stock with a listed future, mapped to its NSE cash instrument.

    Returns rows of:
        {name, symbol, token, tick_size, lot_size}

    `name` is the underlying root (RELIANCE), `symbol` the NSE cash trading
    symbol (RELIANCE-EQ), `token` the NSE cash token used for both quotes
    and the WebSocket subscription.
    """
    df = _master()

    fut = df[(df["exch_seg"] == "NFO") &
             (df["instrumenttype"] == "FUTSTK")].copy()
    if fut.empty:
        raise RuntimeError("No FUTSTK rows in the scrip master — "
                           "cannot build the F&O universe.")
    lot_by_name = (fut.assign(lot=pd.to_numeric(fut["lotsize"],
                                                errors="coerce"))
                      .dropna(subset=["lot"])
                      .groupby("name")["lot"].max().to_dict())
    roots = set(fut["name"].dropna().unique())

    cash = df[(df["exch_seg"] == "NSE") &
              (df["symbol"].astype(str).str.endswith("-EQ"))].copy()
    cash["root"] = cash["symbol"].astype(str).str.replace("-EQ", "",
                                                          regex=False)
    cash = cash[cash["root"].isin(roots)]

    out = []
    seen = set()
    for _, r in cash.iterrows():
        root = str(r["root"])
        if root in seen:
            continue
        seen.add(root)
        try:
            tick = float(r.get("tick_size") or 5) / 100.0  # master is in paise
        except Exception:
            tick = 0.05
        if tick <= 0:
            tick = 0.05
        out.append({
            "name": root,
            "symbol": str(r["symbol"]),
            "token": str(r["token"]),
            "tick_size": tick,
            "lot_size": int(lot_by_name.get(root, 0) or 0),
        })

    out.sort(key=lambda x: x["name"])
    logger.info(f"F&O stock universe built: {len(out)} names "
                f"({len(roots)} futures underlyings in the master).")
    missing = roots - seen
    if missing:
        logger.warning(f"{len(missing)} underlyings had no NSE '-EQ' row and "
                       f"were skipped: {sorted(missing)[:12]}"
                       f"{' ...' if len(missing) > 12 else ''}")
    return out


# ======================================================================
# Pre-open snapshot
# ======================================================================
def _angel_quotes(tokens: list[str]) -> dict:
    """
    FULL-mode getMarketData for a list of NSE tokens, chunked to 50.

    Returns {token: {ltp, prev_close, open, volume, ...}}
    """
    obj = config.SMART
    out = {}
    chunks = [tokens[i:i + QUOTE_CHUNK]
              for i in range(0, len(tokens), QUOTE_CHUNK)]
    for i, chunk in enumerate(chunks, 1):
        for attempt in range(3):
            try:
                api_rate_limiter.wait("getMarketData")
                resp = obj.getMarketData(mode="FULL",
                                         exchangeTokens={"NSE": chunk})
                data = (resp or {}).get("data") or {}
                fetched = data.get("fetched") or []
                for row in fetched:
                    tok = str(row.get("symbolToken"))
                    out[tok] = {
                        "symbol": row.get("tradingSymbol"),
                        "ltp": _f(row.get("ltp")),
                        "prev_close": _f(row.get("close")),
                        "open": _f(row.get("open")),
                        "high": _f(row.get("high")),
                        "low": _f(row.get("low")),
                        "volume": _f(row.get("tradeVolume")),
                        "tot_buy_qty": _f(row.get("totBuyQuan")),
                        "tot_sell_qty": _f(row.get("totSellQuan")),
                        "feed_time": row.get("exchFeedTime"),
                    }
                unfetched = data.get("unfetched") or []
                if unfetched:
                    logger.warning(f"Quote chunk {i}/{len(chunks)}: "
                                   f"{len(unfetched)} tokens unfetched.")
                break
            except Exception as e:
                logger.error(f"getMarketData chunk {i} attempt {attempt+1} "
                             f"failed: {e}")
                time.sleep(1.5 * (attempt + 1))
    logger.info(f"Angel pre-open quotes: {len(out)}/{len(tokens)} tokens "
                f"returned a row.")
    return out


def _f(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _nse_preopen() -> dict:
    """
    Cross-check source: NSE's public pre-open board for the F&O segment.

    Returns {ROOT: {ltp, prev_close, pct, value, qty}} or {} on any failure.
    This is deliberately best-effort — it never raises.
    """
    try:
        s = requests.Session()
        s.headers.update(_NSE_HEADERS)
        s.get(_NSE_HOME, timeout=10)          # seed cookies
        r = s.get(_NSE_PREOPEN_URL, timeout=12)
        r.raise_for_status()
        payload = r.json()
        out = {}
        for row in payload.get("data", []) or []:
            md = row.get("metadata") or {}
            det = (row.get("detail") or {}).get("preOpenMarket") or {}
            sym = str(md.get("symbol") or "").upper()
            if not sym:
                continue
            out[sym] = {
                "ltp": _f(md.get("lastPrice")),
                "prev_close": _f(md.get("previousClose")),
                "pct": _f(md.get("pChange")),
                "qty": _f(det.get("finalQuantity")),
                "value": _f(det.get("totalTurnover")),
            }
        logger.info(f"NSE pre-open board: {len(out)} symbols.")
        return out
    except Exception as e:
        logger.warning(f"NSE pre-open cross-check unavailable: {e}")
        return {}


def preopen_snapshot(universe: list[dict]) -> list[dict]:
    """
    One row per universe name with everything the ranking needs.

    Each row:
        name, symbol, token, tick_size, lot_size,
        auction_price, prev_close, gap_pct, auction_qty, auction_value,
        source
    """
    src = config.STRATEGY.get("preopen_source", "ANGEL_THEN_NSE")
    tokens = [u["token"] for u in universe]

    angel = _angel_quotes(tokens) if src in ("ANGEL", "ANGEL_THEN_NSE") else {}
    nse = _nse_preopen() if src in ("NSE", "ANGEL_THEN_NSE") else {}

    rows = []
    used_nse = 0
    for u in universe:
        a = angel.get(u["token"], {})
        n = nse.get(u["name"], {})

        price = a.get("ltp") or 0.0
        prev = a.get("prev_close") or 0.0
        qty = a.get("volume") or 0.0
        source = "ANGEL"

        # Fall back to NSE only where Angel gave us nothing usable.
        if (price <= 0 or prev <= 0) and n:
            price = n.get("ltp") or price
            prev = n.get("prev_close") or prev
            qty = n.get("qty") or qty
            source = "NSE"
            used_nse += 1
        elif n and price > 0 and n.get("ltp"):
            # Both available — flag a material disagreement, keep Angel.
            if abs(n["ltp"] - price) / price > 0.005:
                logger.warning(f"{u['name']}: pre-open price disagreement "
                               f"Angel {price:.2f} vs NSE {n['ltp']:.2f}")

        value = n.get("value") if n.get("value") else price * qty
        gap = ((price - prev) / prev * 100.0) if (price > 0 and prev > 0) else 0.0

        rows.append({**u,
                     "auction_price": round(price, 2),
                     "prev_close": round(prev, 2),
                     "gap_pct": round(gap, 3),
                     "auction_qty": qty,
                     "auction_value": round(value, 0),
                     "source": source})

    if used_nse:
        logger.warning(f"{used_nse} names took their pre-open price from the "
                       f"NSE board because Angel returned nothing.")
    return rows


# ======================================================================
# Alternate instruments
# ======================================================================
def resolve_option(root: str, reference_price: float, side: str) -> dict | None:
    """
    ATM option on `root` for the nearest (current-month) expiry.
    side 'CE' for a gainer, 'PE' for a loser. Strike closest to the
    opening price, per the strategy note.
    """
    df = _master()
    opt = df[(df["exch_seg"] == "NFO") &
             (df["instrumenttype"] == "OPTSTK") &
             (df["name"] == root)].copy()
    if opt.empty:
        logger.warning(f"{root}: no OPTSTK rows in the scrip master.")
        return None

    opt["expiry_dt"] = pd.to_datetime(opt["expiry"], format="%d%b%Y",
                                      errors="coerce")
    opt["strike_val"] = pd.to_numeric(opt["strike"], errors="coerce") / 100.0
    opt["opt"] = opt["symbol"].astype(str).str.extract(r"(CE|PE)$")
    opt = opt.dropna(subset=["expiry_dt", "strike_val", "opt"])

    today = pd.Timestamp(datetime.now().date())
    fwd = opt[opt["expiry_dt"].dt.normalize() >= today]
    if fwd.empty:
        logger.warning(f"{root}: no live option expiry.")
        return None
    exp = fwd["expiry_dt"].min()

    leg = fwd[(fwd["expiry_dt"] == exp) & (fwd["opt"] == side)]
    if leg.empty:
        logger.warning(f"{root}: no {side} strikes for {exp.date()}.")
        return None

    leg = leg.assign(dist=(leg["strike_val"] - reference_price).abs())
    row = leg.sort_values("dist").iloc[0]
    return {
        "symbol": str(row["symbol"]),
        "token": str(row["token"]),
        "exchange": "NFO",
        "strike": float(row["strike_val"]),
        "lot_size": int(pd.to_numeric(row["lotsize"], errors="coerce") or 0),
        "tick_size": (float(row.get("tick_size") or 5) / 100.0) or 0.05,
        "expiry": exp.strftime("%d%b%Y").upper(),
        "kind": "OPTION",
        "side": side,
    }


def resolve_future(root: str) -> dict | None:
    """Near-month stock future — the short fallback where cash shorting
    is blocked on a scrip."""
    df = _master()
    fut = df[(df["exch_seg"] == "NFO") &
             (df["instrumenttype"] == "FUTSTK") &
             (df["name"] == root)].copy()
    if fut.empty:
        return None
    fut["expiry_dt"] = pd.to_datetime(fut["expiry"], format="%d%b%Y",
                                      errors="coerce")
    fut = fut.dropna(subset=["expiry_dt"])
    today = pd.Timestamp(datetime.now().date())
    fwd = fut[fut["expiry_dt"].dt.normalize() >= today]
    if fwd.empty:
        return None
    row = fwd.sort_values("expiry_dt").iloc[0]
    return {
        "symbol": str(row["symbol"]),
        "token": str(row["token"]),
        "exchange": "NFO",
        "lot_size": int(pd.to_numeric(row["lotsize"], errors="coerce") or 0),
        "tick_size": (float(row.get("tick_size") or 5) / 100.0) or 0.05,
        "expiry": row["expiry_dt"].strftime("%d%b%Y").upper(),
        "kind": "FUTURE",
    }


def option_quote(token: str) -> dict | None:
    """FULL quote for one NFO token — used by the option liquidity checks."""
    try:
        api_rate_limiter.wait("getMarketData")
        resp = config.SMART.getMarketData(mode="FULL",
                                          exchangeTokens={"NFO": [str(token)]})
        fetched = ((resp or {}).get("data") or {}).get("fetched") or []
        if not fetched:
            return None
        r = fetched[0]
        depth = r.get("depth") or {}
        buy = (depth.get("buy") or [{}])[0]
        sell = (depth.get("sell") or [{}])[0]
        return {
            "ltp": _f(r.get("ltp")),
            "bid": _f(buy.get("price")),
            "ask": _f(sell.get("price")),
            "bid_qty": _f(buy.get("quantity")),
            "ask_qty": _f(sell.get("quantity")),
            "oi": _f(r.get("opnInterest")),
            "volume": _f(r.get("tradeVolume")),
        }
    except Exception as e:
        logger.error(f"Option quote failed for {token}: {e}")
        return None


# ======================================================================
# Account
# ======================================================================
def available_balance() -> float | None:
    """Live RMS balance, so the risk budget is recalculated each morning
    rather than sitting fixed at an opening figure."""
    try:
        api_rate_limiter.wait("rmsLimit")
        r = config.SMART.rmsLimit()
        d = (r or {}).get("data") or {}
        for key in ("net", "availablecash", "availableCash",
                    "availableintradaypayin"):
            v = _f(d.get(key), 0.0)
            if v > 0:
                logger.info(f"Live account balance ({key}): Rs {v:,.2f}")
                return v
        logger.warning(f"rmsLimit returned no usable balance field: {d}")
    except Exception as e:
        logger.error(f"rmsLimit failed: {e}")
    return None


def round_to_tick(price: float, tick: float, mode: str = "nearest") -> float:
    """
    Snap a price to the instrument's tick. Trigger prices that are not on a
    tick boundary are rejected by the exchange, which is how protective
    stops go missing.
    """
    if tick <= 0:
        return round(float(price), 2)
    n = float(price) / tick
    if mode == "down":
        import math
        n = math.floor(n + 1e-9)
    elif mode == "up":
        import math
        n = math.ceil(n - 1e-9)
    else:
        n = round(n)
    return round(n * tick, 2)
