import hmac
import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Header, HTTPException
from api.models import PickOut, ReadyOut
from config.settings import settings, PINNACLE
from services.logger import get_logger
from storage import repository as repo
from storage.db import init_db

log = get_logger("api")
_runtime = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _runtime
    if not os.environ.get("API_TOKEN"):
        log.warning("API_TOKEN not set: /beast (your picks) is readable by anyone who knows the URL")
    sport = os.environ.get("SPORT", "basketball").strip().lower()
    if sport == "football":
        from football.runtime import FootballRuntime   # a separate service: own database, own Telegram bot
        _runtime = FootballRuntime()
        _runtime.start()
    elif sport == "tennis":
        from tennis.runtime import TennisRuntime       # a separate service: own database, own Telegram bot
        _runtime = TennisRuntime()
        _runtime.start()
    elif settings.RUN_SCANNER:
        from core.runtime import Runtime
        _runtime = Runtime()
        _runtime.start()  # also runs init_db
    else:
        init_db()
    ping = None
    ext = os.environ.get("RENDER_EXTERNAL_URL", "")
    if ext and os.environ.get("SELF_PING", "1") != "0":
        from services import selfping
        ping = asyncio.create_task(selfping.loop(ext))
        log.info("self-ping on: %s", ext)
    yield
    if ping:
        ping.cancel()
    if _runtime:
        _runtime.stop()


app = FastAPI(title="TheBEAST", lifespan=lifespan)


def _check_token(x_api_key: str = Header(default="")):
    """CLAUDE FIX: when API_TOKEN is set, /beast needs the header `X-API-Key: <token>` (/health and /ready stay open)."""
    token = os.environ.get("API_TOKEN", "")
    if token and not hmac.compare_digest(x_api_key.encode(), token.encode()):
        raise HTTPException(401, "invalid token")


@app.get("/health")
def health():
    return {"status": "ok"}  # static on purpose: Render's health check must never depend on DB or APIs


@app.get("/ready", response_model=ReadyOut)
def ready():
    try:
        n = repo.gold_count(PINNACLE)
        return ReadyOut(status="ready", db=True, gold_pinnacle_rows=n, scanner=bool(_runtime))
    except Exception as e:
        log.error("ready failed: %s", e)
        raise HTTPException(503, "db unavailable")


@app.get("/beast", response_model=list[PickOut], dependencies=[Depends(_check_token)])
def beast(mode: str = None):
    return repo.picks_today(mode)
