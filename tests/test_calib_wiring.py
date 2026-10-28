"""The calibration / rest / price-history wiring is in place."""


def test_bot_has_calib_command():
    from services import telegram_bot as tb
    assert hasattr(tb.TelegramBot, "cmd_calib")
    assert "/calib" in tb.HELP
    assert "calib" in [c for c, _ in tb.COMMANDS]


def test_price_history_table_registered():
    from storage.models import Base
    assert "pin_history" in Base.metadata.tables


def test_pinref_refresh_hook():
    from core.pinref import PinRef
    calls = []
    pr = PinRef(api=None, path="/tmp/_pin_ref_hook_test.json", daily_cap=0)
    pr.on_refresh = lambda lg, ev, sc: calls.append((lg, len(ev), sc))
    pr._after_refresh("nba", [{}], "main")
    assert calls == [("nba", 1, "main")]
    pr.on_refresh = lambda *a: 1 / 0   # a failing hook must never break a refresh
    pr._after_refresh("nba", [], "main")
