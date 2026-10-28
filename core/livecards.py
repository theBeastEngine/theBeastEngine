"""One Telegram card per subject per local day, edited in place (no stream of repeat messages).

put(key, text): first call of the day sends the card (silent), later calls edit it when the text changed; if Telegram
refuses the edit (message deleted / too old) a fresh card is sent. Message ids are kept in the database (kv 'live_cards')
so a restart keeps editing the same card. A card that is not touched for 3 days is forgotten."""
from __future__ import annotations

import hashlib
import json
import logging
import time

log = logging.getLogger(__name__)
KEY = "live_cards"
KEEP_S = 3 * 86400


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]


class LiveCards:
    def __init__(self, load=None, save=None, send=None, edit=None, now=None):
        self._load, self._save = load, save
        self._send, self._edit = send, edit
        self._now = now or time.time
        self.state = None
        self._dirty = False

    def _st(self) -> dict:
        if self.state is None:
            try:
                blob = self._load() if self._load else None
                self.state = json.loads(blob) if blob else {}
            except Exception:
                self.state = {}
        return self.state

    def flush(self):
        try:
            if self._save and self.state is not None and self._dirty:
                now = self._now()
                for k in [k for k, v in self.state.items() if now - v.get("t", 0) > KEEP_S]:
                    del self.state[k]
                self._save(json.dumps(self.state))
                self._dirty = False
        except Exception as e:
            log.warning("live cards not saved: %s", e)

    def _tg_send(self, text, silent):
        if self._send:
            return self._send(text, silent)
        from core import telegram_notify as tn
        from config.settings import settings
        payload = dict(chat_id=settings.TELEGRAM_CHAT_ID, text=text[:4000], parse_mode="HTML")
        if silent:
            payload["disable_notification"] = True
        res = tn.api("sendMessage", **payload)
        return ((res or {}).get("result") or {}).get("message_id")

    def _tg_edit(self, msg_id, text):
        if self._edit:
            return self._edit(msg_id, text)
        from core import telegram_notify as tn
        from config.settings import settings
        from services.client import HttpError
        try:
            tn.api("editMessageText", chat_id=settings.TELEGRAM_CHAT_ID, message_id=msg_id, text=text[:4000], parse_mode="HTML")
            return True
        except HttpError as e:
            s = str(e)
            if "not modified" in s:
                return True
            if "not found" in s or "can't be edited" in s or "message to edit" in s:
                return False
            raise

    def get(self, key: str) -> dict:
        return self._st().get(key) or {}

    def put(self, key: str, text: str, day: str, silent: bool = True, data: dict = None) -> str:
        """Returns 'sent', 'edited' or '' (unchanged / failed). data: small dict stored with the card (for the caller)."""
        if not text:
            return ""
        st = self._st()
        c = st.get(key)
        hh = _h(text)
        now = self._now()
        try:
            if c and c.get("day") == day and c.get("msg_id"):
                if data is not None:
                    c["data"] = data
                    self._dirty = True
                if c.get("h") == hh:
                    c["t"] = now
                    return ""
                try:
                    ok = self._tg_edit(c["msg_id"], text)
                except Exception as e:
                    log.warning("card edit failed (%s): %s", key, e)
                    return ""
                if ok:
                    c.update(h=hh, t=now)
                    self._dirty = True
                    return "edited"
            mid = self._tg_send(text, silent)
            if not mid:
                return ""
            st[key] = dict(day=day, msg_id=mid, h=hh, t=now, data=data if data is not None else {})
            self._dirty = True
            return "sent"
        finally:
            self.flush()
