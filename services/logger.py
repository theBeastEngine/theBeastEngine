import logging
import sys
from config.settings import settings

_done = False


def get_logger(name: str) -> logging.Logger:
    global _done
    if not _done:
        logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
                            stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        # CLAUDE FIX: httpx/httpcore/urllib3 log every request URL at INFO. That floods the Render log (one line
        # per SportyBet call) and would print any URL that carries a secret (e.g. a Telegram bot token).
        for noisy in ("httpx", "httpcore", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _done = True
    return logging.getLogger(name)
