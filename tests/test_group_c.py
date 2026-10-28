"""Group C: perception keeps the caller's ctx; subscription never authorizes a missing id."""
import asyncio
import types

from core import perception, subscription
from core.filter import Ctx


class EmptyFalsyCtx(Ctx):
    """A real Ctx that is falsy (len 0) - `ctx or Ctx()` used to throw it away."""
    seen = []

    def __len__(self):
        return 0

    def remember(self, picks):
        EmptyFalsyCtx.seen.append(list(picks))


def test_perception_uses_a_falsy_ctx_instead_of_replacing_it():
    EmptyFalsyCtx.seen.clear()
    c = EmptyFalsyCtx()
    assert not c
    perception.perception_report([], ctx=c)
    assert EmptyFalsyCtx.seen == [[]]                       # our ctx.remember ran, not a fresh Ctx's


def test_perception_still_builds_a_ctx_when_none():
    kept, reasons = perception.perception_report([{"ev": 0.0}], ctx=None)
    assert kept == [] and reasons.get("below_min_ev") == 1


def test_line_inflation_directions():
    f = perception.line_inflation
    assert f({"market": "totals", "side": "over", "pin_line": 220.5, "line": 219.5}) == 1.0
    assert f({"market": "totals", "side": "under", "pin_line": 220.5, "line": 221.5}) == 1.0
    assert f({"market": "spreads", "side": "home", "pin_line": -5.5, "line": -3.5}) == 2.0
    assert f({"market": "spreads", "side": "away", "pin_line": -5.5, "line": -7.5}) == 2.0
    assert f({"market": "h2h", "side": "home", "pin_line": None, "line": None}) is None


def _owner(monkeypatch, v):
    monkeypatch.setattr(subscription, "SETTINGS", types.SimpleNamespace(TELEGRAM_CHAT_ID=v))


def test_is_authorized_variants(monkeypatch):
    _owner(monkeypatch, "123")
    assert subscription.is_authorized(123) and subscription.is_authorized(" 123 ")
    assert not subscription.is_authorized(124) and not subscription.is_authorized(None)
    _owner(monkeypatch, 123)
    assert subscription.is_authorized("123")
    assert subscription.check_tier(123) == "pro" and subscription.check_tier(5) == "none"


def test_unset_owner_denies_everyone_including_none(monkeypatch):
    for unset in (None, "", "   "):
        _owner(monkeypatch, unset)
        assert not subscription.is_authorized(None)
        assert not subscription.is_authorized(5)
        assert not subscription.is_authorized("")
        assert subscription.check_tier(None) == "none"


def test_require_owner_wrapper(monkeypatch):
    _owner(monkeypatch, "123")
    calls, answers = [], []

    @subscription.require_owner
    async def handler(update, context):
        calls.append(1)
        return "ok"

    async def answer(text, show_alert=False):
        answers.append(text)
    upd = lambda cid, cb=False: types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=cid) if cid is not None else None,
        callback_query=types.SimpleNamespace(answer=answer) if cb else None)
    assert asyncio.run(handler(upd(123), None)) == "ok"
    assert asyncio.run(handler(upd(999), None)) is None
    assert asyncio.run(handler(upd(None), None)) is None
    assert asyncio.run(handler(upd(999, cb=True), None)) is None
    assert calls == [1] and answers == ["Not authorized"]
