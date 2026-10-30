"""Starts the hockey service: database, engine threads, Telegram bot. Selected by SPORT=ice_hockey (api/server.py)."""
from __future__ import annotations

import logging
import os
import threading
import time

from config.settings import settings
from core import livecards
from core.cplog import CPLog
from hockey import cfg
from hockey.bot import HockeyBot
from hockey.engine import Engine
from hockey.pin import PinBook
from hockey.results import Results
from hockey.sporty import SportyFeed
from hockey import tg
from services import odds_api as oa
from services import oddspapi as op
from storage import repository as repo
from storage.db import init_db

log = logging.getLogger("hockey.runtime")
CP_KEY = "hk_cp_log"
KEYBOOK_KEY = "hk_odds_key_state"
CARDS_KEY = "hk_live_cards"


class HockeyRuntime:
    def __init__(self):
        self.engine = None
        self.bot = None
        self._stop = threading.Event()
        self._threads = []

    def build(self):
        oa.book().load = lambda: repo.kv_get(KEYBOOK_KEY)
        oa.book().save = lambda v: repo.kv_set(KEYBOOK_KEY, v)
        papi = op.OddsPapi() if op.keys_from_env() else None
        pin = PinBook(oa_client=oa.OddsAPI(), papi_client=papi, load=repo.kv_get, save=repo.kv_set)
        cards = livecards.LiveCards(load=lambda: repo.kv_get(CARDS_KEY), save=lambda v: repo.kv_set(CARDS_KEY, v),
                                    send=lambda t, silent, reply_to=None: tg.send(t, None, reply_to, silent), edit=lambda mid, t: tg.edit(mid, t))
        self.engine = Engine(repo.kv_get, repo.kv_set, SportyFeed(), pin, Results(oa=oa.OddsAPI()), tg.send, tg.edit, tg.plain, cards,
                             cplog=CPLog(load=lambda: repo.kv_get(CP_KEY), save=lambda v: repo.kv_set(CP_KEY, v)))
        return self.engine

    def _loop(self, name, every, fn, first=0):
        nxt = time.time() + first
        while not self._stop.is_set():
            if time.time() >= nxt:
                nxt = time.time() + every
                try:
                    fn()
                except Exception:
                    log.exception("job %s failed", name)
            self._stop.wait(5)

    def start(self):
        init_db()
        eng = self.build()
        jobs = [("tick", cfg.f("HK_TICK_SEC", 60), eng.tick, 20),
                ("grade", cfg.f("HK_GRADE_MIN", 15) * 60, eng.grade, 600),
                ("hourly", cfg.f("HK_STATUS_MIN", 60) * 60, eng.hourly, 180),
                ("report", 900, eng.daily_report, 240)]
        self._threads = [threading.Thread(target=self._loop, args=jobs[0], name="hk-tick", daemon=True),
                         threading.Thread(target=self._loop_many, args=(jobs[1:],), name="hk-slow", daemon=True)]
        for t in self._threads:
            t.start()
        if settings.TELEGRAM_TOKEN:
            self.bot = HockeyBot(eng)
            threading.Thread(target=self.bot.run_forever, name="hk-bot", daemon=True).start()
        else:
            log.warning("TELEGRAM_BOT_TOKEN not set: hockey alerts and commands disabled")
        log.info("hockey engine started")

    def _loop_many(self, jobs):
        nxt = {j[0]: time.time() + j[3] for j in jobs}
        while not self._stop.is_set():
            for name, every, fn, _first in jobs:
                if time.time() >= nxt[name]:
                    nxt[name] = time.time() + every
                    try:
                        fn()
                    except Exception:
                        log.exception("job %s failed", name)
            self._stop.wait(5)

    def stop(self):
        self._stop.set()
        if self.bot:
            self.bot.stop()
