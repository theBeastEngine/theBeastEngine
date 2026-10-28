# core/subscription.py
from functools import wraps

from config.settings import SETTINGS


def _clean(v) -> str:
    return "" if v is None else str(v).strip()


def is_authorized(user_id) -> bool:
    """True only for the owner's chat. An empty or missing TELEGRAM_CHAT_ID denies everyone.
    CLAUDE FIX: an unset id (None) became the text "None", so is_authorized(None) was True."""
    owner, who = _clean(SETTINGS.TELEGRAM_CHAT_ID), _clean(user_id)
    return bool(owner) and who == owner


def check_tier(user_id) -> str:
    return "pro" if is_authorized(user_id) else "none"


def require_owner(handler):
    """Decorator for Telegram handlers (commands and button callbacks). Non-owners are ignored."""
    @wraps(handler)
    async def wrapper(update, context, *args, **kwargs):
        chat = update.effective_chat
        if chat is None or not is_authorized(chat.id):
            if update.callback_query:
                await update.callback_query.answer("Not authorized", show_alert=True)
            return None
        return await handler(update, context, *args, **kwargs)
    return wrapper
