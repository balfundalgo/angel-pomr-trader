"""
gui.py
======
Control panel for the POMR trader.

The layout is built around the fact that this strategy is over in fifteen
minutes: the left column is set up once before the bell and then ignored,
and the right column is what you actually watch — a countdown to the next
milestone, the ten names with their state, and the activity log.
"""

from __future__ import annotations
from datetime import datetime

import tkinter as tk
from tkinter import ttk
import customtkinter as ctk

import config
from logger import logger, set_gui_sink
from engine import TradingEngine

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

# ---- palette ----
BG = "#eef1f7"
CARD = "#ffffff"
INK = "#111827"
SUB = "#6b7280"
LINE = "#e5e7eb"
ACCENT = "#1d4ed8"
ACCENT_HI = "#1e40af"
OK = "#15803d"
WARN = "#b91c1c"
AMBER = "#b45309"
MUTED = "#9ca3af"

STATE_COLOR = {
    "WAITING_OPEN": SUB, "WAITING_TEST": SUB, "TESTED": AMBER,
    "ENTERED": ACCENT, "CLOSED": INK, "DISQUALIFIED": MUTED,
    "MISSED": MUTED, "SKIPPED": MUTED,
}


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{config.APP_NAME}   v{config.APP_VERSION}")
        self.geometry("1360x860")
        self.minsize(1180, 720)
        self.configure(fg_color=BG)
        self.engine = None
        self._snap = None

        config.load_settings()
        self._build()
        set_gui_sink(self._log_line)
        logger.info(f"{config.APP_NAME} v{config.APP_VERSION} ready.")
        logger.info("Pre-Open Momentum Reclaim — flat by 09:30 every day.")
        self._tick_clock()
        self._tick_monitor()

    # ==================================================================
    # small builders
    # ==================================================================
    def _card(self, parent, title, subtitle=None):
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12,
                         border_width=1, border_color=LINE)
        ctk.CTkLabel(f, text=title.upper(), text_color=ACCENT,
                     font=("Segoe UI Semibold", 11)).pack(
            anchor="w", padx=16, pady=(12, 0))
        if subtitle:
            ctk.CTkLabel(f, text=subtitle, text_color=SUB,
                         font=("Segoe UI", 11)).pack(anchor="w", padx=16,
                                                     pady=(2, 4))
        return f

    def _field(self, parent, label, default="", show=None, hint=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=3)
        ctk.CTkLabel(row, text=label, text_color=INK, font=("Segoe UI", 12),
                     width=168, anchor="w").pack(side="left")
        e = ctk.CTkEntry(row, width=140, show=show, fg_color="#f9fafb",
                         text_color=INK, border_color=LINE, height=30)
        if default != "":
            e.insert(0, str(default))
        e.pack(side="left")
        if hint:
            ctk.CTkLabel(row, text=hint, text_color=MUTED,
                         font=("Segoe UI", 10)).pack(side="left", padx=(8, 0))
        return e

    def _option(self, parent, label, values, default, hint=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=3)
        ctk.CTkLabel(row, text=label, text_color=INK, font=("Segoe UI", 12),
                     width=168, anchor="w").pack(side="left")
        var = ctk.StringVar(value=str(default))
        ctk.CTkOptionMenu(row, values=[str(v) for v in values], variable=var,
                          width=140, height=30, fg_color="#f9fafb",
                          text_color=INK, button_color=ACCENT,
                          button_hover_color=ACCENT_HI, dropdown_fg_color=CARD,
                          dropdown_text_color=INK).pack(side="left")
        if hint:
            ctk.CTkLabel(row, text=hint, text_color=MUTED,
                         font=("Segoe UI", 10)).pack(side="left", padx=(8, 0))
        return var

    def _switch(self, parent, label, default):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=3)
        ctk.CTkLabel(row, text=label, text_color=INK, font=("Segoe UI", 12),
                     width=168, anchor="w").pack(side="left")
        var = ctk.BooleanVar(value=bool(default))
        ctk.CTkSwitch(row, text="", variable=var, width=44,
                      progress_color=ACCENT).pack(side="left")
        return var

    # ==================================================================
    def _build(self):
        self._build_header()

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(0, 14))

        left_wrap = ctk.CTkFrame(body, fg_color="transparent", width=430)
        left_wrap.pack(side="left", fill="y", padx=(0, 14))
        left_wrap.pack_propagate(False)

        footer = ctk.CTkFrame(left_wrap, fg_color="transparent")
        footer.pack(side="bottom", fill="x", pady=(10, 0))
        left = ctk.CTkScrollableFrame(left_wrap, fg_color="transparent")
        left.pack(side="top", fill="both", expand=True)

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True)

        self._build_left(left, footer)
        self._build_right(right)

    # ------------------------------------------------------------------
    def _build_header(self):
        bar = ctk.CTkFrame(self, fg_color="transparent", height=78)
        bar.pack(fill="x", padx=18, pady=(14, 10))

        lhs = ctk.CTkFrame(bar, fg_color="transparent")
        lhs.pack(side="left")
        ctk.CTkLabel(lhs, text="The First Fifteen Minutes", text_color=INK,
                     font=("Segoe UI Semibold", 24)).pack(anchor="w")
        ctk.CTkLabel(lhs, text="Pre-Open Momentum Reclaim  ·  NSE F&O equities "
                               "·  Angel One SmartAPI",
                     text_color=SUB, font=("Segoe UI", 12)).pack(anchor="w")

        rhs = ctk.CTkFrame(bar, fg_color="transparent")
        rhs.pack(side="right")
        self.lbl_clock = ctk.CTkLabel(rhs, text="--:--:--", text_color=INK,
                                      font=("Consolas", 26))
        self.lbl_clock.pack(anchor="e")
        self.lbl_next = ctk.CTkLabel(rhs, text="idle", text_color=SUB,
                                     font=("Segoe UI", 12))
        self.lbl_next.pack(anchor="e")

        mid = ctk.CTkFrame(bar, fg_color="transparent")
        mid.pack(side="right", padx=24)
        self.lbl_mode = ctk.CTkLabel(mid, text=" SCAN_ONLY ", text_color="#ffffff",
                                     fg_color=MUTED, corner_radius=6,
                                     font=("Segoe UI Semibold", 12))
        self.lbl_mode.pack(anchor="e", pady=(4, 4))
        self.lbl_feed = ctk.CTkLabel(mid, text="feed: offline", text_color=SUB,
                                     font=("Segoe UI", 11))
        self.lbl_feed.pack(anchor="e")

    # ------------------------------------------------------------------
    def _build_left(self, left, footer):
        s = config.STRATEGY
        c = config.CREDENTIALS

        cred = self._card(left, "Angel One credentials",
                          "Stay on this machine, saved to settings.json")
        cred.pack(fill="x", pady=(0, 10))
        self.e_client = self._field(cred, "Client ID", c.get("client_id", ""))
        self.e_apikey = self._field(cred, "API Key", c.get("api_key", ""))
        self.e_mpin = self._field(cred, "MPIN", c.get("mpin", ""), show="*")
        self.e_totp = self._field(cred, "TOTP secret", c.get("totp_secret", ""),
                                  show="*")
        ctk.CTkLabel(cred, text="", height=4).pack()

        run = self._card(left, "Run mode")
        run.pack(fill="x", pady=(0, 10))
        self.v_mode = self._option(run, "Mode", ["SCAN_ONLY", "PAPER", "LIVE"],
                                   config.TRADING_MODE)
        ctk.CTkLabel(run, text="SCAN_ONLY writes the day's ten names and stops. "
                              "Run it for ten\nsessions and check the output by "
                              "hand before anything else.",
                     text_color=SUB, font=("Segoe UI", 10), justify="left").pack(
            anchor="w", padx=16, pady=(2, 10))

        tm = self._card(left, "The morning timetable")
        tm.pack(fill="x", pady=(0, 10))
        self.e_scan = self._field(tm, "Scan time", s["scan_time"], hint="09:08:30")
        self.e_feed = self._field(tm, "Feed connect", s["feed_time"])
        self.e_open = self._field(tm, "Market open", s["open_time"])
        self.e_check = self._field(tm, "Early check", s["check_seconds"],
                                   hint="seconds  ·  0 = act from the open")
        self.e_last = self._field(tm, "Last entry", s["last_entry_time"])
        self.e_sq = self._field(tm, "Square off", s["square_off_time"])
        ctk.CTkLabel(tm, text="", height=4).pack()

        wl = self._card(left, "Choosing the ten")
        wl.pack(fill="x", pady=(0, 10))
        self.e_topn = self._field(wl, "Top N per side", s["top_n"])
        self.e_mingap = self._field(wl, "Min move %", s["min_gap_pct"])
        self.e_minpx = self._field(wl, "Min price", s["min_price"], hint="Rs")
        self.e_minval = self._field(wl, "Min auction value", s["min_auction_value"],
                                    hint="Rs")
        self.v_enfval = self._switch(wl, "Enforce thin filter",
                                     s["enforce_auction_value"])
        self.v_src = self._option(wl, "Pre-open source",
                                  ["ANGEL", "NSE", "ANGEL_THEN_NSE"],
                                  s["preopen_source"])
        ctk.CTkLabel(wl, text="Ex-dividend / split names to remove today "
                              "(comma separated):",
                     text_color=SUB, font=("Segoe UI", 10)).pack(anchor="w",
                                                                 padx=16,
                                                                 pady=(6, 2))
        self.e_excl = ctk.CTkEntry(wl, fg_color="#f9fafb", text_color=INK,
                                   border_color=LINE, height=30)
        self.e_excl.insert(0, s.get("exclusions", ""))
        self.e_excl.pack(fill="x", padx=16, pady=(0, 12))

        sg = self._card(left, "Size, stop and limits")
        sg.pack(fill="x", pady=(0, 10))
        self.v_livebal = self._switch(sg, "Use live balance",
                                      s["use_live_balance"])
        self.e_cap = self._field(sg, "Capital (fallback)", s["capital"])
        self.e_risk = self._field(sg, "Risk per trade %", s["risk_pct"])
        self.e_minstop = self._field(sg, "Min stop %", s["min_stop_pct"],
                                     hint="widen")
        self.e_maxtest = self._field(sg, "Max test depth %", s["max_test_pct"],
                                     hint="skip")
        self.e_maxpos_pct = self._field(sg, "Max position %", s["max_position_pct"])
        self.e_buf = self._field(sg, "Clearly-through %", s["cross_buffer_pct"])
        self.e_ticks = self._field(sg, "...min ticks", s["min_cross_ticks"])
        self.e_maxtr = self._field(sg, "Max trades / day", s["max_trades"])
        self.e_maxop = self._field(sg, "Max open at once", s["max_positions"])
        self.e_losscap = self._field(sg, "Daily loss cap %", s["daily_loss_cap_pct"])
        ctk.CTkLabel(sg, text="", height=4).pack()

        ins = self._card(left, "Instrument")
        ins.pack(fill="x", pady=(0, 10))
        self.v_inst = self._option(ins, "Trade", ["STOCK", "OPTION"],
                                   s["instrument"])
        self.v_short = self._option(ins, "Short side via", ["CASH", "FUTURES"],
                                    s["short_instrument"])
        self.e_ospread = self._field(ins, "Max option spread %",
                                     s["option_max_spread_pct"])
        self.e_ooi = self._field(ins, "Min open interest", s["option_min_oi"])
        self.v_otraded = self._switch(ins, "Must have traded today",
                                      s["option_require_traded"])
        self.v_exsl = self._switch(ins, "Resting stop at broker",
                                   s["place_exchange_sl"])
        self.e_slip = self._field(ins, "Paper slippage %", s["paper_slippage_pct"])
        ctk.CTkLabel(ins, text="", height=6).pack()

        # ---- fixed footer ----
        btns = ctk.CTkFrame(footer, fg_color="transparent")
        btns.pack(fill="x")
        self.btn_start = ctk.CTkButton(btns, text="Start", height=40,
                                       font=("Segoe UI Semibold", 14),
                                       fg_color=ACCENT, hover_color=ACCENT_HI,
                                       command=self._start)
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.btn_stop = ctk.CTkButton(btns, text="Stop", height=40,
                                      font=("Segoe UI Semibold", 14),
                                      fg_color=MUTED, hover_color=WARN,
                                      command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=(5, 0))
        ctk.CTkButton(footer, text="Save settings", height=32,
                      fg_color="#0f766e", hover_color="#115e59",
                      command=self._save).pack(fill="x", pady=(8, 0))

    # ------------------------------------------------------------------
    def _build_right(self, right):
        top = ctk.CTkFrame(right, fg_color="transparent")
        top.pack(fill="x")

        pnl = self._card(top, "Session P&L")
        pnl.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.lbl_pnl = ctk.CTkLabel(pnl, text="\u20b9 0.00", text_color=INK,
                                    font=("Segoe UI Semibold", 34), anchor="w")
        self.lbl_pnl.pack(anchor="w", padx=16, pady=(2, 0))
        self.lbl_pnl_sub = ctk.CTkLabel(pnl, text="Realized \u20b90  ·  "
                                                  "Unrealized \u20b90",
                                        text_color=SUB, font=("Segoe UI", 12),
                                        anchor="w")
        self.lbl_pnl_sub.pack(anchor="w", padx=16, pady=(0, 14))

        stats = self._card(top, "Session")
        stats.pack(side="left", fill="both", expand=True)
        self.lbl_stats = ctk.CTkLabel(stats, text="—", text_color=INK,
                                      font=("Consolas", 12), justify="left",
                                      anchor="w")
        self.lbl_stats.pack(anchor="w", padx=16, pady=(2, 14), fill="x")

        # ---- watchlist table ----
        wl = self._card(right, "Watchlist",
                        "All ten stay live until 09:30 — nothing is dropped "
                        "for looking weak")
        wl.pack(fill="x", pady=(12, 0))
        self._build_table(wl)

        logc = self._card(right, "Activity log")
        logc.pack(fill="both", expand=True, pady=(12, 0))
        self.txt = ctk.CTkTextbox(logc, fg_color="#0b1220",
                                  text_color="#dbeafe", font=("Consolas", 12),
                                  corner_radius=8)
        self.txt.pack(fill="both", expand=True, padx=14, pady=(4, 14))

    def _build_table(self, parent):
        cols = ("name", "side", "gap", "open", "extreme", "ltp", "entry",
                "stop", "qty", "state", "pnl")
        heads = ("Stock", "Side", "Gap %", "Open", "Extreme", "LTP", "Entry",
                 "Stop", "Qty", "State", "P&L")
        widths = (110, 62, 70, 86, 86, 86, 86, 86, 62, 116, 96)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("POMR.Treeview", background=CARD, foreground=INK,
                        fieldbackground=CARD, rowheight=25, borderwidth=0,
                        font=("Consolas", 11))
        style.configure("POMR.Treeview.Heading", background="#f3f4f6",
                        foreground=SUB, borderwidth=0,
                        font=("Segoe UI Semibold", 10))
        style.map("POMR.Treeview.Heading", background=[("active", "#e5e7eb")])
        style.map("POMR.Treeview", background=[("selected", "#dbeafe")],
                  foreground=[("selected", INK)])

        holder = ctk.CTkFrame(parent, fg_color=CARD)
        holder.pack(fill="x", padx=14, pady=(4, 14))
        self.tree = ttk.Treeview(holder, columns=cols, show="headings",
                                 height=11, style="POMR.Treeview")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="e" if c not in
                             ("name", "side", "state") else "w")
        for state, colr in STATE_COLOR.items():
            self.tree.tag_configure(state, foreground=colr)
        self.tree.tag_configure("profit", foreground=OK)
        self.tree.tag_configure("loss", foreground=WARN)
        self.tree.pack(fill="x")

    # ==================================================================
    # inputs
    # ==================================================================
    def _collect(self):
        config.CREDENTIALS.update({
            "client_id": self.e_client.get().strip(),
            "api_key": self.e_apikey.get().strip(),
            "mpin": self.e_mpin.get().strip(),
            "totp_secret": self.e_totp.get().strip(),
        })
        config.TRADING_MODE = self.v_mode.get()
        s = config.STRATEGY
        s["scan_time"] = self.e_scan.get().strip()
        s["feed_time"] = self.e_feed.get().strip()
        s["open_time"] = self.e_open.get().strip()
        chk = int(float(self.e_check.get()))
        if chk < 0:
            raise ValueError("early check cannot be negative")
        if chk > 780:   # 09:28 is 780s after the open
            raise ValueError("early check falls after the last-entry time")
        s["check_seconds"] = chk
        s["last_entry_time"] = self.e_last.get().strip()
        s["square_off_time"] = self.e_sq.get().strip()

        s["top_n"] = int(self.e_topn.get())
        s["min_gap_pct"] = float(self.e_mingap.get())
        s["min_price"] = float(self.e_minpx.get())
        s["min_auction_value"] = float(self.e_minval.get())
        s["enforce_auction_value"] = bool(self.v_enfval.get())
        s["preopen_source"] = self.v_src.get()
        s["exclusions"] = self.e_excl.get().strip()

        s["use_live_balance"] = bool(self.v_livebal.get())
        s["capital"] = float(self.e_cap.get())
        s["risk_pct"] = float(self.e_risk.get())
        s["min_stop_pct"] = float(self.e_minstop.get())
        s["max_test_pct"] = float(self.e_maxtest.get())
        s["max_position_pct"] = float(self.e_maxpos_pct.get())
        s["cross_buffer_pct"] = float(self.e_buf.get())
        s["min_cross_ticks"] = int(self.e_ticks.get())
        s["max_trades"] = int(self.e_maxtr.get())
        s["max_positions"] = int(self.e_maxop.get())
        s["daily_loss_cap_pct"] = float(self.e_losscap.get())

        s["instrument"] = self.v_inst.get()
        s["short_instrument"] = self.v_short.get()
        s["option_max_spread_pct"] = float(self.e_ospread.get())
        s["option_min_oi"] = float(self.e_ooi.get())
        s["option_require_traded"] = bool(self.v_otraded.get())
        s["place_exchange_sl"] = bool(self.v_exsl.get())
        s["paper_slippage_pct"] = float(self.e_slip.get())

    def _save(self):
        try:
            self._collect()
        except ValueError as e:
            logger.error(f"Invalid input, nothing saved: {e}")
            return
        if config.save_settings():
            logger.info(f"Settings saved to {config.SETTINGS_FILE}")
        else:
            logger.error("Could not write settings.json")

    def _start(self):
        try:
            self._collect()
        except ValueError as e:
            logger.error(f"Invalid input: {e}")
            return
        if config.STRATEGY["check_seconds"] == 0:
            logger.warning("Early check is 0 — there is no warm-up delay. "
                           "Entries may fire on the first ticks after 09:15, "
                           "when a test-and-reclaim can be spread noise "
                           "rather than a real move. Watch the "
                           "'clearly-through' buffer.")
        if config.TRADING_MODE == "LIVE":
            logger.warning("LIVE MODE — real orders will be sent.")
        config.save_settings()
        self.engine = TradingEngine(status_cb=self._status)
        self.engine.start()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal", fg_color=WARN)
        self.lbl_mode.configure(
            text=f" {config.TRADING_MODE} ",
            fg_color={"SCAN_ONLY": MUTED, "PAPER": AMBER,
                      "LIVE": WARN}[config.TRADING_MODE])

    def _stop(self):
        if self.engine:
            self.engine.stop()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled", fg_color=MUTED)

    # ==================================================================
    # rendering
    # ==================================================================
    def _status(self, snap):
        self._snap = snap

    def _log_line(self, line):
        self.after(0, lambda: (self.txt.insert("end", line + "\n"),
                               self.txt.see("end")))

    def _tick_clock(self):
        now = datetime.now()
        self.lbl_clock.configure(text=now.strftime("%H:%M:%S"))
        self.lbl_next.configure(text=self._next_milestone(now))
        self.after(250, self._tick_clock)

    def _next_milestone(self, now):
        s = config.STRATEGY
        marks = [(config.today_at(s["scan_time"]), "scan"),
                 (config.today_at(s["feed_time"]), "feed"),
                 (config.open_dt(), "market open"),
                 (config.check_dt(), "the check"),
                 (config.last_entry_dt(), "last entry"),
                 (config.square_off_dt(), "square off")]
        for t, label in marks:
            if now < t:
                secs = int((t - now).total_seconds())
                return f"{label} in {secs // 60:02d}:{secs % 60:02d}"
        return "session over"

    def _tick_monitor(self):
        try:
            if self.engine is not None:
                snap = self.engine.live_snapshot()
                self._render(snap)
        except Exception:
            pass
        self.after(700, self._tick_monitor)

    def _render(self, s):
        total = s["total"]
        self.lbl_pnl.configure(text=f"\u20b9 {total:,.2f}",
                               text_color=OK if total > 0 else
                               (WARN if total < 0 else INK))
        self.lbl_pnl_sub.configure(
            text=f"Realized \u20b9{s['realized']:,.2f}  ·  "
                 f"Unrealized \u20b9{s['unrealized']:,.2f}  ·  "
                 f"{total / s['capital'] * 100:+.2f}% of capital"
            if s["capital"] else "")

        halt = f"HALTED — {s['halt_reason']}" if s["halted"] else "running"
        self.lbl_stats.configure(
            text=(f"phase     {s['phase']}\n"
                  f"capital   \u20b9{s['capital']:,.0f}\n"
                  f"trades    {s['trades_taken']} / {s['max_trades']}"
                  f"    open {s['open_positions']}\n"
                  f"status    {halt}"))

        self.lbl_feed.configure(
            text=f"feed: {'live' if s['feed'] else 'offline'}  ·  "
                 f"{s['ticks']:,} ticks",
            text_color=OK if s["feed"] else SUB)

        self.tree.delete(*self.tree.get_children())
        for r in s["rows"]:
            pnl = r["pnl"]
            tag = r["state"]
            if r["state"] in ("ENTERED", "CLOSED") and pnl:
                tag = "profit" if pnl > 0 else "loss"
            self.tree.insert("", "end", tags=(tag,), values=(
                r["name"],
                r["side"],
                f"{r['gap_pct']:+.2f}",
                f"{r['open']:.2f}" if r["open"] else "—",
                f"{r['extreme']:.2f}" if r["extreme"] else "—",
                f"{r['ltp']:.2f}" if r["ltp"] else "—",
                f"{r['entry']:.2f}" if r["entry"] else "—",
                f"{r['stop']:.2f}" if r["stop"] else "—",
                r["qty"] or "—",
                r["state"],
                f"{pnl:+,.0f}" if pnl else "—",
            ))


def run():
    App().mainloop()


if __name__ == "__main__":
    run()
