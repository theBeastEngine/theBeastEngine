import time
from datetime import datetime, timedelta, timezone

from core import papiprobe as P
from services import oddspapi as op
from services.client import HttpError


class FakeClient:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def call(self, path, params=None, base=None):
        self.calls.append((path, dict(params or {}), base))
        r = self.routes[path]
        if isinstance(r, Exception):
            raise r
        return r(params) if callable(r) else r


NOW = time.time()
START = int(NOW - 1800)


def _live(odds=True, status=1):
    return [dict(fixtureId="id1", status=dict(live=True, statusId=status), tournament=dict(tournamentName="PBA"),
                 participants=dict(participant1Name="Ginebra", participant2Name="Phoenix"), startTime=START,
                 scores=dict(result=dict(participant1Score=65, participant2Score=45)),
                 clock=dict(currentPeriod=3, currentTime="05:10"),
                 bookmakers=dict(pinnacle=dict(hasOdds=odds, staleOdds=False, suspended=False)))]


def _odds(changed_ms):
    q = lambda i: {"bookmaker": "pinnacle", "price": 1.9, "active": True, "marketActive": True, "mainLine": True, "changedAt": changed_ms}
    return dict(odds=dict(pinnacle={"a": q(1), "b": q(2)}))


def test_live_prices_detected_as_live():
    c = FakeClient({"/fixtures/live": _live(), "/fixtures/odds": _odds(int((NOW - 40) * 1000))})
    txt = "\n".join(P.probe_live(c, now=NOW))
    assert "LIVE" in txt and "ARE available" in txt and "65-45" in txt
    assert c.calls[0][2] == op.BASE5 and c.calls[1][1]["fixtureId"] == "id1"


def test_frozen_prices_detected():
    c = FakeClient({"/fixtures/live": _live(), "/fixtures/odds": _odds((START - 3600) * 1000)})
    txt = "\n".join(P.probe_live(c, now=NOW))
    assert "FROZEN" in txt and "only frozen" in txt


def test_no_odds_or_nothing_live_or_error_is_said_plainly():
    assert "no odds" in "\n".join(P.probe_live(FakeClient({"/fixtures/live": _live(odds=False)}), now=NOW)).lower()
    assert "Nothing in play" in "\n".join(P.probe_live(FakeClient({"/fixtures/live": []}), now=NOW))
    txt = "\n".join(P.probe_live(FakeClient({"/fixtures/live": HttpError(401, "bad key")}), now=NOW))
    assert "failed" in txt and "401" in txt


class FakeBeast:
    class pinref:
        data = {"leagues": {}, "extras": {"pba": {"events": [
            dict(event_id="papi-id77", home="Ginebra", away="Phoenix",
                 commence=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=6))]}}}


def test_settlement_probe_reads_final_score(monkeypatch):
    import storage.repository as repo
    monkeypatch.setattr(repo, "all_picks", lambda n=0: [], raising=False)
    st = dict(status=dict(statusName="Finished"), scores=dict(result=dict(participant1Score=90, participant2Score=80)),
              settlements=[dict(status="WIN"), dict(status="LOSE")])
    c = FakeClient({"/fixtures/settlement": st})
    txt = "\n".join(P.probe_settle(c, FakeBeast()))
    assert "90-80" in txt and "settlement works" in txt and c.calls[0][1]["fixtureId"] == "id77"


def test_settlement_probe_with_no_finished_game():
    class Empty:
        class pinref:
            data = {"leagues": {}, "extras": {}}
    txt = "\n".join(P.probe_settle(FakeClient({}), Empty()))
    assert "No finished game" in txt


def test_run_never_raises():
    c = FakeClient({"/fixtures/live": RuntimeError("boom")})
    txt = P.run(FakeBeast(), "live", client=c)
    assert "failed" in txt


def test_probe_never_touches_the_engines_own_client(monkeypatch):
    monkeypatch.setenv("ODDSPAPI_KEYS", "k1,k2,k3,k4")
    monkeypatch.delenv("ODDSPAPI_V5_KEYS", raising=False)
    c = P.v5_client()
    assert c.keys == ["k1", "k2"]
    monkeypatch.setenv("ODDSPAPI_V5_KEYS", "v5a; v5b ,v5c")
    assert P.v5_client().keys == ["v5a", "v5b", "v5c"]

    class B:
        class pinref:
            _papi = object()
            data = {"leagues": {}, "extras": {}}
    before = B.pinref._papi

    def rejecting(url, params=None, **k):
        raise HttpError(401, "invalid apiKey")
    monkeypatch.setattr(op, "OddsPapi", lambda keys=None, **kw: type(c)(keys=keys, get=rejecting, sleep=lambda s: None, spacing=0))
    txt = P.run(B, "live")
    assert B.pinref._papi is before
    assert "needs its own key" in txt
