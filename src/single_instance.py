"""
single_instance.py
==================
Refuses to let two copies of the app run at once.

This is not housekeeping. Every instance runs its own order-book watcher, its
own reconciler and its own position polling, so a second copy silently
doubles the API load — which is exactly how a connection that is comfortably
inside Angel's limits starts being refused for "exceeding access rate". It
also means two engines can both decide to act on the same account.

A bound local socket is used rather than a PID file because the operating
system releases it automatically if the process dies, so a crash can never
leave a stale lock that blocks the next morning's session.
"""

from __future__ import annotations
import socket

from logger import logger

_PORT = 47319          # arbitrary, local only
_sock = None


def acquire() -> bool:
    """True if this process now owns the lock, False if another holds it."""
    global _sock
    if _sock is not None:
        return True
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _PORT))
        s.listen(1)
        _sock = s
        return True
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return False


def release():
    global _sock
    if _sock is not None:
        try:
            _sock.close()
        except Exception:
            pass
        _sock = None


def warn_if_duplicate() -> bool:
    """Returns True when it is safe to proceed."""
    if acquire():
        return True
    logger.critical(
        "ANOTHER COPY OF THIS APP IS ALREADY RUNNING. Two instances share one "
        "Angel account: they double every API call, which gets the connection "
        "rate limited, and both will try to trade. Close the other window "
        "before starting this one.")
    return False
