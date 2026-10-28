import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

import api.server as server
from config.settings import PINNACLE
from core.pinref import PinRef
from core.scheduler import Scheduler


def test_beast_endpoint_requires_token_when_set(monkeypatch):
    monkeypatch.setattr(server, "repo", SimpleNamespace(picks_today=lambda mode=None: []))
    client = TestClient(server.app)
    monkeypatch.setenv("API_TOKEN", "s3cret")
    assert client.get("/beast").status_code == 401
    assert client.get("/beast", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/beast", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/health").status_code == 200  # health check stays open
    monkeypatch.delenv("API_TOKEN")
    assert client.get("/beast").status_code == 200  # unset = old behaviour (a warning is logged at startup)


def test_scheduler_loop_survives_unexpected_errors():
    class Loop:
        n = 0

        def is_set(self):
            self.n += 1
            return self.n > 3

        def wait(self, t):
            pass

    beast = SimpleNamespace(paused=False, run_live=lambda: [], run_prematch=lambda: [], run_sim=lambda: [])
    s = Scheduler(beast)
    s._stop = Loop()
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("x")

    s._backup_if_due = boom
    s._run([], with_backup=True)  # must not raise
    assert len(calls) == 3


def test_pinref_cache_defaults_next_to_the_database(monkeypatch):
    monkeypatch.delenv("PIN_REF_PATH", raising=False)
    monkeypatch.setenv("DB_PATH", "/data/beast.db")
    assert PinRef().path == "/data/pin_ref.json"
    monkeypatch.setenv("PIN_REF_PATH", "/tmp/custom.json")
    assert PinRef().path == "/tmp/custom.json"


def test_two_threads_spend_credits_once(tmp_path):
    c = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = [{"id": "g", "home_team": "A", "away_team": "B", "commence_time": c, "bookmakers": [
        {"key": PINNACLE, "markets": [{"key": "h2h", "outcomes": [
            {"name": "A", "price": 1.8}, {"name": "B", "price": 2.0}]}]}]}]

    class SlowAPI:
        calls = 0

        def fetch(self, key, markets=None, books=None, regions="eu"):
            time.sleep(0.2)
            SlowAPI.calls += 1
            return raw

    ref = PinRef(api=SlowAPI(), path=str(tmp_path / "p.json"), max_age_min=60)
    ts = [threading.Thread(target=ref.ensure_fresh, args=("nba",)) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert SlowAPI.calls == 1
