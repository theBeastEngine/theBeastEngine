from core import table
from football import cards as fb
from tennis import cards as tn


def _rows(blocks):
    return [r for blk in blocks if blk.startswith("<pre>") for r in blk[5:-6].split("\n")]


def test_first_name_skips_short_tags_and_keeps_the_line():
    assert table.first_name("Basilashvili") == "Basilashvili"
    assert table.first_name("CR Flamengo RJ") == "Flamengo"
    assert table.first_name("Atletico Mineiro MG") == "Atletico"
    assert table.first_name("CR Vasco da Gama RJ +0.5") == "Vasco +0.5"
    assert table.first_name("Van de Zandschulp") == "Zandschulp"
    assert table.first_name("Over 20.5") == "Over 20.5" and table.first_name("Draw") == "Draw"
    assert table.first_name("Alcaraz 2:0") == "Alcaraz 2:0"


def test_football_and_tennis_cards_share_one_narrow_aligned_table():
    f = [("MATCH RESULT (1X2)", [("Atletico Mineiro MG", 2.55, 2.56, -0.002), ("Draw", 3.50, 3.66, -0.043), ("Cruzeiro EC MG", 2.70, 2.98, -0.095)])]
    t = [("MATCH WINNER", [("Basilashvili", 7.30, 10.78, -0.323), ("Alcaraz", 1.11, 1.10, 0.007)])]
    assert fb.table_text(f) == table.table_text(f) and tn.table_text(t) == table.table_text(t)
    fr, tr = _rows(fb.table_text(f)), _rows(tn.table_text(t))
    for rows in (fr, tr):
        assert all(len(r) <= 28 for r in rows)                  # fits one line on a phone
        assert len({r.index("|") for r in rows}) == 1 and len({r.rindex("|") for r in rows}) == 1
    assert fr[0].startswith("Atletico ") and fr[2].startswith("Cruzeiro ") and tr[0].startswith("Basilash") and tr[1].startswith("Alcaraz ")
    assert tr[0].endswith("-32.3%") and tr[1].endswith("+0.7%")


def test_teams_sharing_a_first_word_stay_distinguishable():
    rows = _rows(table.table_text([("1X2", [("Manchester United", 2.0, 2.0, 0.0), ("Manchester City", 3.0, 3.0, 0.0)])]))
    assert rows[0].split("  ")[0] != rows[1].split("  ")[0]
    assert all(len(r) <= 28 for r in rows)
