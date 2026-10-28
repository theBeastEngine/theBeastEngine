"""Remembers which API keys are dead (out of quota) ACROSS restarts and moves them to the back of the line.

Nothing is ever removed from the key list on Render: a dead key is only tried LAST, and again after DEAD_HOURS (24 h), so a key
whose monthly allowance refills comes back into use by itself. Only a short hash of a key is stored, never the key.
Used by services/odds_api.py (The Odds API) and services/oddspapi.py (OddsPapi). Env KEYBOOK=0 turns the memory off."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

KEY = "key_state"
_lock = threading.Lock()
_io = {"load": None, "save": None}
_state = {"d": None}


def enabled() -> bool:
    return os.environ.get("KEYBOOK", "1").strip() != "0"


def dead_hours() -> float:
    try:
        return float(os.environ.get("KEY_DEAD_HOURS", 24))
    except ValueError:
        return 24.0


def configure(load=None, save=None):
    """Called once by the engine: where to read / write the remembered state (the kv store)."""
    with _lock:
        _io["load"], _io["save"] = load, save
        _state["d"] = None


def _h(key: str) -> str:
    return hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:10]


def _d() -> dict:
    if _state["d"] is None:
        try:
            blob = _io["load"]() if _io["load"] else None
            _state["d"] = json.loads(blob) if blob else {}
        except Exception:
            _state["d"] = {}
    return _state["d"]


def _flush():
    try:
        if _io["save"]:
            _io["save"](json.dumps(_state["d"] or {}))
    except Exception:
        pass


def is_dead(name: str, key: str, now: float = None) -> bool:
    if not enabled():
        return False
    now = time.time() if now is None else now
    with _lock:
        return (_d().get(name, {}).get(_h(key), {}).get("until") or 0) > now


def order(name: str, keys: list, now: float = None) -> list:
    """Working keys first (original order), dead ones after. Same keys, nothing dropped."""
    now = time.time() if now is None else now
    alive = [k for k in keys if not is_dead(name, k, now)]
    return alive + [k for k in keys if k not in alive]


def mark_dead(name: str, key: str, now: float = None):
    if not enabled():
        return
    now = time.time() if now is None else now
    with _lock:
        r = _d().setdefault(name, {}).setdefault(_h(key), {})
        r.update(until=now + dead_hours() * 3600, last4=str(key)[-4:], t=now)
        _flush()


def mark_ok(name: str, key: str, remaining=None, now: float = None):
    """A call worked: the key is alive again; remember how many credits it reports left and how many calls we made."""
    if not enabled():
        return
    now = time.time() if now is None else now
    with _lock:
        r = _d().setdefault(name, {}).setdefault(_h(key), {})
        was_dead = (r.get("until") or 0) > now
        r.update(last4=str(key)[-4:], until=0, t=now, n=int(r.get("n", 0)) + 1)
        if remaining is not None:
            try:
                r["remaining"] = float(remaining)
            except (TypeError, ValueError):
                pass
        if was_dead or r["n"] % 10 == 1:
            _flush()


def snapshot(name: str, keys: list, now: float = None) -> list:
    """[(last4, status 'working'|'dead', dead_until_epoch, credits_left or None, calls_seen)] for every key of the list."""
    now = time.time() if now is None else now
    out = []
    with _lock:
        for k in keys:
            r = _d().get(name, {}).get(_h(k), {})
            dead = (r.get("until") or 0) > now
            out.append((str(k)[-4:], "dead" if dead else "working", r.get("until") if dead else None,
                        r.get("remaining"), int(r.get("n", 0))))
    return out
