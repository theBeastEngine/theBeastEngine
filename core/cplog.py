"""Which checkpoint (T-60 / T-30 / T-15 / T-5) is best? Every PLAY NOW market gets its verdict (play / skip) recorded at each
checkpoint with the odds then; later the market's real result grades them: 'Play' verdicts show win rate and return, 'Skip'
verdicts show how many winners the checkpoint vetoed. Small samples first, so /qcheck shows the counts next to the rates."""
from __future__ import annotations

import json
import logging
import time

log = logging.getLogger(__name__)
KEY = "cp_log"
KINDS = ("t60", "final", "t15", "last")
NAMES = {"t60": "T-60", "final": "T-30", "t15": "T-15", "last": "T-5"}
MAX_KEEP = 3000


class CPLog:
    def __init__(self, load=None, save=None, now=None):
        self._load, self._save = load, save
        self._now = now or time.time
        self.data = None

    def _d(self) -> dict:
        if self.data is None:
            try:
                blob = self._load() if self._load else None
                self.data = json.loads(blob) if blob else {}
            except Exception:
                self.data = {}
        return self.data

    def flush(self):
        try:
            d = self._d()
            if len(d) > MAX_KEEP:
                for k in sorted(d, key=lambda k: d[k].get("t", 0))[:len(d) - MAX_KEEP]:
                    del d[k]
            if self._save:
                self._save(json.dumps(d))
        except Exception as e:
            log.warning("checkpoint log not saved: %s", e)

    def record(self, pid, kind, verdict, odds=None) -> bool:
        """verdict: 'play' or 'skip'. One record per pick; the checkpoints fill its 'v' map."""
        if pid is None or kind not in KINDS or verdict not in ("play", "skip"):
            return False
        r = self._d().setdefault(str(pid), dict(t=int(self._now()), v={}))
        if kind in r["v"]:
            return False
        r["v"][kind] = [verdict, round(float(odds), 3) if odds else None]
        self.flush()
        return True

    def rows(self, result_of):
        """[(name, n_play, won, return % of the plays, n_skip, skipped winners)] for checkpoints with any graded pick."""
        out = []
        for k in KINDS:
            np_, w, ret, ns, sw = 0, 0, 0.0, 0, 0
            for pid, r in self._d().items():
                v = r["v"].get(k)
                if not v:
                    continue
                try:
                    res = result_of(pid)
                except Exception:
                    res = None
                if res not in ("won", "lost"):
                    continue
                if v[0] == "play":
                    np_ += 1
                    if res == "won":
                        w += 1
                        ret += (v[1] or 2.0) - 1
                    else:
                        ret -= 1
                else:
                    ns += 1
                    sw += 1 if res == "won" else 0
            if np_ or ns:
                out.append((NAMES[k], np_, w, (100.0 * ret / np_) if np_ else 0.0, ns, sw))
        return out
