"""
logger.py
=========
Activity logging. Writes a dated file, echoes to stdout, and forwards each
line to the GUI so the operator sees the morning happen in real time.

Trade / order / scan CSVs are written by their owning modules (order_manager
and scanner) so each file has a stable, purpose-built schema.
"""

import logging
import config

_gui_sink = None  # callable(str) set by the GUI


def set_gui_sink(fn):
    global _gui_sink
    _gui_sink = fn


class _GuiHandler(logging.Handler):
    def emit(self, record):
        if _gui_sink:
            try:
                _gui_sink(self.format(record))
            except Exception:
                pass


def _build():
    lg = logging.getLogger("pomr")
    lg.setLevel(logging.INFO)
    if lg.handlers:
        return lg
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%H:%M:%S")

    fh = logging.FileHandler(config.log_file(), encoding="utf-8")
    fh.setFormatter(fmt)
    lg.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    lg.addHandler(sh)

    gh = _GuiHandler()
    gh.setFormatter(fmt)
    lg.addHandler(gh)
    return lg


logger = _build()
