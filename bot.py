"""Standalone runner (no web): scheduler + Telegram bot in one process. `python bot.py`"""
import signal
import time
from core.runtime import Runtime


def _term(*_):  # Render sends SIGTERM on redeploy: stop the scheduler/bot cleanly
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _term)
    rt = Runtime()
    rt.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        rt.stop()
