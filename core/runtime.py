import threading
from config.settings import settings
from core import telegram_notify
from core.beast import Beast
from core.scheduler import Scheduler
from services.logger import get_logger
from storage.db import init_db

log = get_logger("runtime")


class Runtime:
    """Starts DB, gold fill (0 credits), scheduler and Telegram bot. One place used by web, bot.py and the worker script."""

    def __init__(self):
        self.beast = Beast(notify=telegram_notify.notify if settings.TELEGRAM_TOKEN else None)
        self.scheduler = Scheduler(self.beast)
        self.bot = None

    def start(self):
        init_db()
        try:
            log.info("gold pinnacle rows: %s", self.beast.ensure_gold())
        except Exception as e:
            log.error("ensure_gold failed: %s", e)
        self.scheduler.start()
        if settings.TELEGRAM_TOKEN:
            from services.telegram_bot import TelegramBot
            self.bot = TelegramBot(self.beast)
            threading.Thread(target=self.bot.run_forever, name="beast-bot", daemon=True).start()
        else:
            log.warning("TELEGRAM_BOT_TOKEN not set: alerts and commands disabled")

    def stop(self):
        self.scheduler.stop()
        if self.bot:
            self.bot.stop()
