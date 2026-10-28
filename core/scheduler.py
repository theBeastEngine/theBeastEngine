import os
import threading
import time
from datetime import datetime, timezone
from core.modes import MODES, PREMATCH, LIVE, SIM
from core import digest

DIGEST = "digest"
AUTOLINK = "autolink"
WRAPUP = "wrapup"
TRACK = "track"
GRADE = "grade"
CLOSE = "close"
from services.logger import get_logger

log = get_logger("scheduler")


class Scheduler:
    """Live every SCAN_SECONDS on its own thread; prematch/sim (slow) share a second thread; nightly Pinnacle backup.
    CLAUDE FIX: a slow prematch/Monte Carlo run can no longer delay the live scan."""

    BACKUP_RETRY_SEC = 1800

    def __init__(self, beast):
        self.beast = beast
        self._stop = threading.Event()
        self._thread = None
        self._threads = []
        self._last_backup_day = None
        self._backup_retry_at = 0.0
        self._next = {}

    def _jobs(self):
        b = self.beast
        return {LIVE: (MODES[LIVE].every_sec, b.run_live), PREMATCH: (MODES[PREMATCH].every_sec, b.run_prematch),
                SIM: (MODES[SIM].every_sec, b.run_sim),
                DIGEST: (max(60.0, digest.every_min() * 60), getattr(b, "send_digest", lambda: None)),
                AUTOLINK: (float(os.environ.get("AUTOLINK_EVERY_H", 6)) * 3600, getattr(b, "auto_link", lambda: None)),
                WRAPUP: (900.0, getattr(b, "wrapup_if_due", lambda: None)),
                TRACK: (float(os.environ.get("TRACK_EVERY_SEC", 300)), getattr(b, "run_tracker", lambda: None)),
                GRADE: (max(600.0, float(os.environ.get("GRADE_EVERY_MIN", 60)) * 60), getattr(b, "run_grader", lambda: None)),
                CLOSE: (max(10.0, float(os.environ.get("CLOSING_SWEEP_SEC", 30))), getattr(b, "run_closing_sweep", lambda: None))}

    def _backup_if_due(self):
        now = datetime.now(timezone.utc)
        if now.hour != 3 or self._last_backup_day == now.date() or time.time() < self._backup_retry_at:
            return
        from storage import repository as repo
        try:
            log.info("pinnacle backup: %s rows", repo.backup_pinnacle())
            self._last_backup_day = now.date()
        except Exception as e:
            # CLAUDE FIX: was retried every 5 seconds for the whole 03:00 hour
            self._backup_retry_at = time.time() + self.BACKUP_RETRY_SEC
            log.error("backup failed: %s", e)

    def _run(self, names, with_backup=False):
        jobs = self._jobs()
        while not self._stop.is_set():
            try:  # CLAUDE FIX: any unexpected error used to kill this thread silently (no more scans until redeploy)
                now = time.time()
                for name in names:
                    every, fn = jobs[name]
                    if self.beast.paused:
                        break
                    if now >= self._next.get(name, 0):
                        self._next[name] = now + every
                        try:
                            fn()
                        except Exception as e:
                            log.exception("job %s failed: %s", name, e)
                if with_backup:
                    self._backup_if_due()
            except Exception as e:
                log.exception("scheduler loop error: %s", e)
            self._stop.wait(5)

    def _loop(self):
        """Single-thread mode (kept for compatibility / tests)."""
        self._run([LIVE, PREMATCH, SIM], with_backup=True)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if digest.every_min() > 0:  # first summary one interval after start (DIGEST_EVERY_MIN=0 turns it off)
            self._next[DIGEST] = time.time() + digest.every_min() * 60
        slow = [PREMATCH, SIM] + ([DIGEST] if digest.every_min() > 0 else [])
        if float(os.environ.get("AUTOLINK_EVERY_H", 6)) > 0:   # 0 turns the automatic league linking off
            slow.append(AUTOLINK)
            self._next[AUTOLINK] = time.time() + 600       # first look 10 minutes after start-up
        if os.environ.get("WRAPUP", "1").strip() != "0":   # nightly wrap-up (WRAPUP=0 turns it off)
            slow.append(WRAPUP)
            self._next[WRAPUP] = time.time() + 300
        self._next[TRACK] = time.time() + 60          # first look one minute after start-up
        self._next[GRADE] = time.time() + 900         # first grading 15 minutes after start-up
        self._threads = [
            threading.Thread(target=self._run, args=([LIVE],), name="beast-live", daemon=True),
            threading.Thread(target=self._run, args=(slow, True), name="beast-slow", daemon=True),
            threading.Thread(target=self._run, args=([TRACK, GRADE],), name="beast-track", daemon=True),
        ]
        if os.environ.get("CLOSING_SWEEP", "1").strip() != "0":    # CLOSING_SWEEP=0 turns the end-of-game look off
            self._threads.append(threading.Thread(target=self._run, args=([CLOSE],), name="beast-close", daemon=True))
        for t in self._threads:
            t.start()
        self._thread = self._threads[0]

    def stop(self):
        self._stop.set()
