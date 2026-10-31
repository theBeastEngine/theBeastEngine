"""Telegram transport for american football (uses the service's own TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)."""
from __future__ import annotations

from core import telegram_notify as tn
from config.settings import settings
from services.client import HttpError


def send(text, markup=None, reply_to=None, silent=False):
    payload = dict(chat_id=settings.TELEGRAM_CHAT_ID, text=text[:4000], parse_mode="HTML")
    if markup:
        payload["reply_markup"] = markup
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    if silent:
        payload["disable_notification"] = True
    res = tn.api("sendMessage", **payload)
    return ((res or {}).get("result") or {}).get("message_id")


def edit(msg_id, text, markup=None):
    payload = dict(chat_id=settings.TELEGRAM_CHAT_ID, message_id=msg_id, text=text[:4000], parse_mode="HTML",
                   reply_markup=markup or {"inline_keyboard": []})
    try:
        tn.api("editMessageText", **payload)
        return True
    except HttpError as e:
        s = str(e)
        if "not modified" in s:
            return True
        if "not found" in s or "can't be edited" in s or "message to edit" in s:
            return False
        raise


def plain(text, silent=False):
    return send(text, None, None, silent)
