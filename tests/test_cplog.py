from core.cplog import CPLog


def test_checkpoint_grading():
    c = CPLog()
    c.record(1, "t60", "play", 1.9); c.record(1, "last", "skip", None)
    c.record(2, "t60", "play", 2.0); c.record(2, "last", "play", 1.8)
    c.record(3, "t60", "play", 1.9); c.record(3, "last", "skip")
    assert c.record(1, "t60", "skip") is False                          # first verdict of a checkpoint stands
    res = {"1": "won", "2": "lost", "3": "lost"}
    rows = {r[0]: r for r in c.rows(lambda pid: res.get(str(pid)))}
    assert rows["T-60"][1:3] == (3, 1) and abs(rows["T-60"][3] - (0.9 - 1 - 1) / 3 * 100) < 1e-6
    assert rows["T-5"][1:3] == (1, 0) and rows["T-5"][4:] == (2, 1)       # 2 skips, 1 of them would have won


def test_open_picks_not_graded():
    c = CPLog()
    c.record(9, "t60", "play", 1.9)
    assert c.rows(lambda pid: None) == []
