from core.pinref import _same


def test_real_euroleague_names_from_the_logs_match():
    assert _same("KK Crvena zvezda Belgrade", "KK Crvena zvezda")
    assert _same("Bayern Munich", "FC Bayern M\u00fcnchen")
    assert _same("Virtus Bologna", "Virtus Segafredo Bologna")
    assert _same("Olimpia Milano", "Pallacanestro Olimpia Milano")
    assert _same("Maccabi Tel-Aviv", "Maccabi Tel Aviv")
    assert _same("Dubai Basketball", "Dubai Basketball")
    assert _same("Panathinaikos BC", "Panathinaikos")


def test_different_teams_do_not_match():
    assert not _same("Real Madrid", "KK Partizan Belgrade")
    assert not _same("Los Angeles Lakers", "Los Angeles Clippers")
    assert not _same("Panathinaikos BC", "Fenerbahce Istanbul")
    assert not _same("Bayern Munich", "Virtus Bologna")
    assert not _same("Boston Celtics", "Brooklyn Nets")


def test_clubs_that_share_a_last_word_are_not_the_same_club():
    # these used to match on the last word alone: a derby then matched with home and away swapped
    assert not _same("Paris Basketball", "Dubai Basketball")
    assert not _same("Maccabi Tel Aviv", "Hapoel Tel Aviv")
    assert not _same("KK Partizan Belgrade", "KK Crvena zvezda Belgrade")
    assert not _same("Real Madrid", "Atletico Madrid")


def test_short_forms_still_match():
    assert _same("Los Angeles Lakers", "LA Lakers")
    assert _same("Oklahoma City Thunder", "OKC Thunder")
    assert _same("Portland Trail Blazers", "Trail Blazers")
    assert _same("Boston Celtics", "Celtics")
    assert _same("Fenerbahce Istanbul", "Fenerbahce Beko Istanbul")


def test_derby_with_swapped_home_and_away_does_not_match():
    from datetime import datetime, timedelta, timezone
    from core.pinref import PinRef
    t = (datetime.now(timezone.utc) + timedelta(hours=5)).replace(tzinfo=None)
    ref = PinRef(path="/tmp/_pin_derby_test.json")
    ref.data = {"leagues": {"euroleague": {"fetched_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                                            "events": [{"home": "Hapoel Tel Aviv", "away": "Maccabi Tel Aviv",
                                                        "commence": t.isoformat(), "markets": {}}]}}, "spend": {}}
    swapped = {"league": "euroleague", "home": "Maccabi Tel Aviv", "away": "Hapoel Tel Aviv", "commence": t}
    same = {"league": "euroleague", "home": "Hapoel Tel Aviv", "away": "Maccabi Tel Aviv", "commence": t}
    assert ref.match(swapped) is None
    assert ref.match(same) is not None


def test_names_from_the_second_log_match():
    assert _same("Baskonia Vitoria-Gasteiz", "Saski Baskonia")
    assert _same("BC Olympiakos Piraeus", "Olympiacos")
    assert _same("Besiktas JK", "Be\u015fikta\u015f J.K.")
    assert _same("Anadolu Efes SK", "Anadolu Efes")
    assert _same("FC Barcelona", "FC Barcelona B\u00e0squet")


def test_shared_long_word_does_not_merge_different_clubs():
    assert not _same("Paris Basketball", "Dubai Basketball")
    assert not _same("Real Madrid", "Atletico Madrid")
    assert not _same("Olympiacos", "Panathinaikos")
    assert not _same("Fenerbahce Istanbul", "Anadolu Efes Istanbul")


def test_names_that_failed_in_the_live_logs_now_match():
    assert _same("BC Olympiakos Piraeus", "Olympiacos")
    assert _same("Anadolu Efes SK", "Anadolu Efes")
    assert _same("Baskonia Vitoria-Gasteiz", "Saski Baskonia")
    assert _same("Besiktas JK", "Be\u015fikta\u015f J.K.")
    assert _same("FC Barcelona", "FC Barcelona B\u00e0squet")
    assert _same("Zalgiris Kaunas", "\u017dalgiris")


def test_two_clubs_that_share_a_city_are_not_the_same_club():
    assert not _same("Melbourne United", "South East Melbourne Phoenix")
    assert not _same("KK Partizan Belgrade", "KK Crvena zvezda Belgrade")
    assert not _same("Fenerbahce Istanbul", "Anadolu Efes Istanbul")
    assert not _same("Real Madrid", "Atletico Madrid")
    assert not _same("Olimpia Milano", "Virtus Bologna")
