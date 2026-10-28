import logging
import os
import time
import threading
from contextlib import contextmanager
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from config.settings import settings
from storage.models import Base
from utils import dburl

log = logging.getLogger("beast.db")
_lock = threading.Lock()
_engine = None
_Session = None
_backend = "sqlite"


def _make_remote(url):
    """Neon / Postgres engine, or None when it cannot be used (the caller then falls back to SQLite so the
    engine keeps scanning; picks will not survive a deploy in that case and the log says so loudly)."""
    try:
        eng = create_engine(dburl.normalize(url), pool_pre_ping=True, pool_size=5, max_overflow=5,
                            pool_recycle=240, connect_args={"connect_timeout": 15})
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        return eng
    except Exception as e:
        log.error("DATABASE_URL is set but unusable (%s: %s). Falling back to SQLite: picks and results "
                  "will NOT survive a deploy until this is fixed.", type(e).__name__, dburl.redact(str(e))[:200])
        return None


def get_engine():
    global _engine, _Session, _backend
    with _lock:
        if _engine is None:
            url = dburl.configured()
            if url and settings.DB_PATH != ":memory:":
                eng = _make_remote(url)
                if eng is not None:
                    _engine = eng
                    _Session = sessionmaker(bind=eng, expire_on_commit=False)
                    _backend = "postgres"
                    log.info("storage: Postgres (Neon) is active; picks and results survive deploys")
                    return _engine
            _backend = "sqlite"
            path = settings.DB_PATH
            kw = {"connect_args": {"check_same_thread": False, "timeout": 30}, "pool_pre_ping": True}
            if path == ":memory:":
                kw["poolclass"] = StaticPool      # one shared in-memory DB across threads (tests)
                kw.pop("pool_pre_ping")
            else:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            eng = create_engine(f"sqlite:///{path}", **kw)

            @event.listens_for(eng, "connect")
            def _pragmas(conn, _rec):
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

            _engine = eng
            _Session = sessionmaker(bind=eng, expire_on_commit=False)
        return _engine


def is_remote() -> bool:
    """True when the Postgres (Neon) database is the active backend."""
    get_engine()
    return _backend == "postgres"


def backend_name() -> str:
    return "Neon Postgres" if is_remote() else "SQLite (wiped on every deploy)"


def _add_missing_columns(eng) -> None:
    """create_all() never alters existing tables. If an older DB (e.g. V14.5 picks table) is on the
    disk, add any column the models have that the table lacks (always as nullable: SQLite limitation)."""
    insp = inspect(eng)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have:
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(eng.dialect)}'
                log.warning("schema upgrade: %s", ddl)
                with eng.begin() as c:
                    c.execute(text(ddl))


def init_db(retries: int = 6) -> None:
    """Idempotent, safe if two processes boot together (retries on 'database is locked')."""
    eng = get_engine()
    for i in range(retries):
        try:
            Base.metadata.create_all(eng)
            _add_missing_columns(eng)
            return
        except OperationalError:
            if i == retries - 1:
                raise
            time.sleep(0.5 * (i + 1))


@contextmanager
def get_session():
    get_engine()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def reset_engine() -> None:
    """For tests: drop cached engine so a new DB_PATH takes effect."""
    global _engine, _Session, _backend
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _Session = None
        _backend = "sqlite"
