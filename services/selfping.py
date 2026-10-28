"""Pings our own public /health so a Render Free service stays awake. Off unless RENDER_EXTERNAL_URL is set."""
import asyncio
import urllib.request
from services.logger import get_logger
log = get_logger("selfping")
INTERVAL_SEC = 600
def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "beast-selfping"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status
async def loop(base_url, interval=INTERVAL_SEC):
    url = base_url.rstrip("/") + "/health"
    while True:
        await asyncio.sleep(interval)
        try:
            status = await asyncio.to_thread(_get, url)
            log.info("self-ping %s -> %s", url, status)
        except Exception as e:
            log.warning("self-ping failed: %s", e)
