from types import SimpleNamespace

import services.telegram_bot as tb


class FakeRepo:
    def __init__(self, pick=None):
        self.pick, self.status_calls, self.took_calls, self.bank = pick, [], [], []

    def get_pick(self, pid):
        return self.pick

    def set_status(self, pid, s):
        self.status_calls.append((pid, s))

    def set_took_odds(self, pid, o):
        self.took_calls.append((pid, o))

    def bankroll_set(self, a, src):
        self.bank.append(a)

    def bankroll_current(self):
        return 100000.0


def _pick(**kw):
    p = dict(id=7, league="nba", home="A&B", away="C", market="h2h", selection="home", line=None,
             book_odds=2.0, ev=0.05, status="new", took_odds=None, min_odds=1.95, book="sportybet")
    p.update(kw)
    return p


def _bot(monkeypatch, repo):
    monkeypatch.setattr(tb, "repo", repo)
    return tb.TelegramBot(SimpleNamespace(paused=False))


def test_replies_are_html_safe(monkeypatch):
    sent = []
    monkeypatch.setattr(tb, "settings", SimpleNamespace(TELEGRAM_CHAT_ID="1"))
    monkeypatch.setattr(tb, "is_authorized", lambda c: True)
    monkeypatch.setattr(tb.tn, "send", lambda text, markup=None, chat_id=None: sent.append(text) or True)
    bot = _bot(monkeypatch, FakeRepo())
    bot.process_update({"message": {"text": "/start", "chat": {"id": 1}}})
    assert "<id>" not in sent[0] and "&lt;id&gt;" in sent[0]


def test_pick_line_escapes_team_names():
    assert "A&amp;B" in tb._line(_pick())


def test_skip_never_erases_a_placed_bet(monkeypatch):
    repo = FakeRepo(_pick(status="placed"))
    toast, _ = _bot(monkeypatch, repo).handle_callback("skip:7")
    assert repo.status_calls == [] and "placed" in toast.lower()


def test_placed_button_always_sets_status(monkeypatch):
    repo = FakeRepo(_pick())
    _bot(monkeypatch, repo).handle_callback("placed:7")
    assert (7, "placed") in repo.status_calls and repo.took_calls == [(7, 2.0)]
    repo2 = FakeRepo(_pick(took_odds=2.1))
    _bot(monkeypatch, repo2).handle_callback("placed:7")
    assert (7, "placed") in repo2.status_calls and repo2.took_calls == []


def test_bad_button_data_is_handled(monkeypatch):
    assert _bot(monkeypatch, FakeRepo(_pick())).handle_callback("placed:xyz")[0] == "Bad button"


def test_took_marks_placed_and_warns_below_min(monkeypatch):
    repo = FakeRepo(_pick())
    out = _bot(monkeypatch, repo).handle_text("/took 7 1.90")
    assert "below" in out.lower() and (7, "placed") in repo.status_calls and repo.took_calls == [(7, 1.9)]
    assert "between" in _bot(monkeypatch, repo).handle_text("/took 7 500").lower()


def test_bankroll_validation(monkeypatch):
    repo = FakeRepo()
    bot = _bot(monkeypatch, repo)
    assert "above 0" in bot.handle_text("/bankroll 0") and repo.bank == []
    bot.handle_text("/bankroll 250,000")
    assert repo.bank == [250000.0]
