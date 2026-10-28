"""DATABASE_URL handling for Neon (pure Python, no database needed)."""
import pytest
from utils import dburl

NEON = ("postgresql://neondb_owner:secretpw@ep-x-pooler.c-7.eu-central-1.aws.neon.tech/neondb"
        "?sslmode=require&channel_binding=require")


def test_normalize_adds_driver_and_drops_channel_binding():
    out = dburl.normalize(NEON)
    assert out.startswith("postgresql+psycopg2://neondb_owner:secretpw@ep-x-pooler")
    assert "channel_binding" not in out
    assert "sslmode=require" in out


def test_normalize_accepts_postgres_scheme_and_whitespace():
    assert dburl.normalize("  postgres://u:p@h/db  ").startswith("postgresql+psycopg2://u:p@h/db")


def test_normalize_rejects_non_postgres():
    with pytest.raises(ValueError):
        dburl.normalize("mysql://u:p@h/db")
    with pytest.raises(ValueError):
        dburl.normalize("")


def test_is_postgres():
    assert dburl.is_postgres(NEON)
    assert not dburl.is_postgres("sqlite:///x.db")
    assert not dburl.is_postgres("")


def test_redact_hides_password():
    msg = f"could not connect to {NEON} timeout"
    out = dburl.redact(msg)
    assert "secretpw" not in out and "***@" in out
    assert dburl.redact(None) == ""


def test_configured_reads_environment_at_call_time(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert dburl.configured() == ""
    monkeypatch.setenv("DATABASE_URL", "  " + NEON + " ")
    assert dburl.configured() == NEON
