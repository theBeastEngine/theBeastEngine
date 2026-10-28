"""Pure-Python helpers for DATABASE_URL (no SQLAlchemy import, so they are easy to test).

Neon hands out `postgresql://user:pass@host/db?sslmode=require&channel_binding=require`.
SQLAlchemy needs the driver in the scheme, and `channel_binding` is dropped because older libpq builds reject it
(sslmode=require still encrypts the connection)."""
import os
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

_DROP_PARAMS = {"channel_binding"}


def configured() -> str:
    """The raw DATABASE_URL from the environment ('' when unset). Read at call time, never cached."""
    return (os.environ.get("DATABASE_URL") or "").strip()


def is_postgres(url: str) -> bool:
    return bool(url) and url.split(":", 1)[0].lower() in ("postgres", "postgresql", "postgresql+psycopg2")


def normalize(url: str) -> str:
    """Return a SQLAlchemy URL for psycopg2, with unsupported query parameters removed."""
    url = (url or "").strip()
    if not is_postgres(url):
        raise ValueError("DATABASE_URL must start with postgres:// or postgresql://")
    parts = urlsplit(url)
    scheme = "postgresql+psycopg2"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in _DROP_PARAMS])
    return urlunsplit((scheme, parts.netloc, parts.path, query, parts.fragment))


def redact(text: str) -> str:
    """Hide the password in any message that may contain a connection URL."""
    return re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1***@", text or "")
