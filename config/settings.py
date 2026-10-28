"""Central settings. Lock constants are fixed on purpose (see tests/test_locks.py)."""
import os
from dataclasses import dataclass

# ---- LOCKED constants (never read from env) ----
TOP_N = 5               # prematch: top 5 EV
LIVE_TOP_N = 3          # live: top 3 EV
SIM_TOP_N = 3           # simulation: top 3 EV
SCAN_SECONDS = 120      # live scan every 2 min (SportyBet live feed, 0 credits)
MIN_EV = 0.04           # minimum edge to alert
MIN_EV_LIVE = 0.04      # live floor (same as prematch until you decide otherwise)
MAX_ODDS = 5.0          # liability filter: longshot prices are mostly model noise
MAX_BOOK_MARGIN = 0.12  # margin-trap filter: book's own two-way overround above this = distorted market
MAX_EV_SANITY = 0.20    # above this = almost surely a bad line, skip
LIVE_NO_REPEAT_MIN = 30
MC_SIMS = 10_000
STAKE_ROUND = 100       # stakes rounded to N100
MAX_STAKE_PCT = 0.05    # max stake = 5% of CURRENT bankroll
GOLD_EXPECTED_ROWS = 1890  # 14 leagues x 15 markets x 9 tiers (per source)
PINNACLE = "pinnacle"


def _s(name, default=""):
    return os.getenv(name, default).strip()


def _f(name, default):
    v = os.getenv(name, "").strip()
    return float(v) if v else float(default)   # empty value (e.g. FLAT_STAKE=) falls back to the default


def _i(name, default):
    v = os.getenv(name, "").strip()
    return int(v) if v else int(default)


def _b(name, default=False):
    return os.getenv(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


def _list(name, default=""):
    return [x.strip() for x in _s(name, default).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    DB_PATH: str
    RUN_SCANNER: bool
    ODDS_API_KEYS: list
    SOFT_BOOKS: list        # native scrapers (core/bookmakers/<book>.py), 0 credits
    API_SOFT_BOOKS: list    # Odds API books used only for the gold-table fill (verify keys against the API docs)
    TELEGRAM_TOKEN: str
    TELEGRAM_CHAT_ID: str
    BANKROLL_START: float
    FLAT_STAKE: float
    MAX_BETS_PER_BOOK_PER_DAY: int
    GOLD_BACKUP_PATH: str
    LOG_LEVEL: str
    MIN_EV: float = MIN_EV
    MIN_EV_LIVE: float = MIN_EV_LIVE


def load_settings() -> Settings:
    db_path = _s("DB_PATH", "/data/beast.db")
    # the backup must live on the same persistent disk as the DB, otherwise a redeploy wipes it
    default_backup = (os.path.join(os.path.dirname(os.path.abspath(db_path)), "pinnacle_reference.json")
                      if db_path != ":memory:" else "data/pinnacle_reference.json")
    return Settings(
        DB_PATH=db_path,
        RUN_SCANNER=_b("RUN_SCANNER", False),
        ODDS_API_KEYS=_list("ODDS_API_KEYS") or _list("ODDS_API_KEY"),
        SOFT_BOOKS=_list("SOFT_BOOKS", "sportybet"),   # only SportyBet is confirmed live; add others via env
        API_SOFT_BOOKS=_list("API_SOFT_BOOKS", "betway,unibet_eu,williamhill,marathonbet,betsson,nordicbet,coolbet,betclic_fr,winamax_fr"),
        TELEGRAM_TOKEN=_s("TELEGRAM_BOT_TOKEN") or _s("TELEGRAM_TOKEN"),
        TELEGRAM_CHAT_ID=_s("TELEGRAM_CHAT_ID"),
        BANKROLL_START=_f("BANKROLL_START", 100000),
        FLAT_STAKE=_f("FLAT_STAKE", 5000),
        MAX_BETS_PER_BOOK_PER_DAY=_i("MAX_BETS_PER_BOOK_PER_DAY", 999),
        GOLD_BACKUP_PATH=_s("GOLD_BACKUP_PATH", default_backup),
        LOG_LEVEL=_s("LOG_LEVEL", "INFO"),
    )


settings = load_settings()
SETTINGS = settings          # alias used by perception/validator/subscription
SOFT_BOOKS = settings.SOFT_BOOKS
