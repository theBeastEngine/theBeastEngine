"""The injury / line-movement / confirmation wiring is in place."""
import inspect


def test_finalize_uses_confirmation():
    from core import filter as flt
    src = inspect.getsource(flt.finalize)
    assert "confirm.confirm(" in src and "gates.confirm_persistent(" not in src


def test_beast_has_guard():
    from core import beast
    cls = beast.Beast if hasattr(beast, "Beast") else next(v for v in vars(beast).values()
                                                          if inspect.isclass(v) and hasattr(v, "run_prematch"))
    assert hasattr(cls, "_guard") and hasattr(cls, "_hold_reason")
    src = inspect.getsource(cls.run_prematch)
    assert "finalize(self._guard(cands), PREMATCH)" in src
    assert "self._track_positions(cands)" in src   # tracking still sees every candidate


def test_alert_shows_injury_note():
    from core import telegram_notify as tn
    base = dict(id=1, mode="prematch", league="nba", home="A", away="B", event_id="e", market="spreads",
                selection="home", line=-5.5, fair_odds=1.9, min_odds=1.95, book="sportybet", odds=2.0, ev=0.05,
                stake=5000, p=0.56, side="home", rank_in_game=1, priority="Medium")
    txt = tn.format_game_alert([dict(base, injury_note="Injuries: A: Out Joe <b>.")], bankroll=100000)
    assert "Injuries: A: Out Joe &lt;b&gt;." in txt
    assert "Injuries" not in tn.format_game_alert([base], bankroll=100000)
