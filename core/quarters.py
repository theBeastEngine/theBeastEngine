"""Quarter outlook: for a live game, what each REMAINING quarter is expected to look like.
A FORECAST from the model (pre-game Pinnacle line + live score and pace). It is not a bet: no price is checked.
In Q1 it covers Q1-Q4, in Q2 Q2-Q4, in Q3 Q3-Q4, in Q4 only Q4 (two-half leagues: the halves)."""
import html

TOTAL_LEAN = 0.04     # pace this far above/below the pre-game scoring rate = a total lean
SIDE_LEAN_PTS = 1.0   # expected margin (points) in one period needed to name a side


def _label(n, k):
    return ("Half %d" % k) if n == 2 else ("Q%d" % k)


def outlook(league, period, elapsed, home_score, away_score, mu, T, n, L, halftime=False, source="pinnacle", break_after=None):
    """period: 1..n in play; halftime=True means a break; break_after = the period that just ended (default n // 2 = half-time),
    so the next period is break_after + 1. Returns dict or None."""
    cur = home_score + away_score
    diff = home_score - away_score
    if (not halftime and L and 1 <= period < n and elapsed is not None and elapsed >= L):
        # CLAUDE FIX: the quarter clock is at 0:00 but the feed still says "in play". That moment IS the break after this
        # quarter. It used to divide by zero (outlook failed, so the Q1/half-time/Q3 break scores were never saved and the
        # next message lost its "Q1 Break / Earlier Qn" rows); now it is a break, so the exact score is kept.
        halftime, break_after = True, period
    if halftime:
        done = break_after if break_after else n // 2
        first, frac_in_first = done + 1, 1.0
        played = done / n
    else:
        if period < 1 or period > n:
            return None
        e = min(max(elapsed, 0.0), L)
        first, frac_in_first = period, ((L - e) / L if L else 0.0)
        played = (((period - 1) + e / L) / n) if L else 0.0
    rem = 1.0 - played
    if rem <= 0.01:
        return None
    blended = rem * T + cur                   # same pace blend the live total model uses (full-game points)
    rem_pts = rem * blended                   # expected points still to come
    ratio = blended / T if T else 1.0
    shares = []
    for k in range(first, n + 1):
        fr = frac_in_first if k == first else 1.0
        shares.append((k, fr / n))            # share of the whole game inside this period still to be played
    tot_share = sum(s for _, s in shares)
    if not shares or tot_share <= 0:
        return None
    qs = []
    for k, s in shares:
        pts = rem_pts * s / tot_share
        marg = mu * s                          # pre-game expected margin spread evenly over time
        t_lean = "Over" if ratio >= 1 + TOTAL_LEAN else "Under" if ratio <= 1 - TOTAL_LEAN else None
        thr = max(SIDE_LEAN_PTS * (s * n), 1e-9)
        s_lean = "home" if marg >= thr else "away" if marg <= -thr else None
        t_str = abs(ratio - 1) / TOTAL_LEAN if t_lean else 0.0
        s_str = abs(marg) / thr if s_lean else 0.0
        best = None
        if t_lean or s_lean:
            best = ("total", t_lean) if t_str >= s_str else ("side", s_lean)
        qs.append(dict(label=_label(n, k) + (" (rest)" if k == first and not halftime and frac_in_first < 0.97 else ""),
                       exp_pts=pts, exp_margin=marg, total_lean=t_lean, side_lean=s_lean, best=best))
    return dict(league=league, period=period, halftime=halftime, home_score=home_score, away_score=away_score,
                ratio=ratio, quarters=qs, source=source, break_after=break_after if halftime else None, mu=mu, T=T, rem=rem, diff=diff, minutes_played=played * n * L / 60.0,
                minutes_total=n * L / 60.0)


def reading(it):
    """Plain-language summary of one game: the pace so far and what it means for the remaining quarters."""
    cur = it["home_score"] + it["away_score"]
    mp, mt = it.get("minutes_played", 0), it.get("minutes_total", 0)
    pct = round(100 * (it["ratio"] - 1))
    labels = [q["label"].replace(" (rest)", "") for q in it["quarters"]]
    if len(labels) == 1:
        span = "the rest of " + labels[0]
    elif len(labels) == 2:
        span = "the rest of %s and for %s" % (labels[0], labels[1])
    else:
        span = "the rest of " + ", ".join(labels[:-1]) + " and " + labels[-1]
    base = "%d points after about %d of %d minutes" % (cur, round(mp), round(mt))
    if it["ratio"] >= 1 + TOTAL_LEAN:
        txt = "%s is faster than the pre-game line expected, about %d%% over. So it leans Over for %s." % (base, pct, span)
    elif it["ratio"] <= 1 - TOTAL_LEAN:
        txt = "%s is slower than the pre-game line expected, about %d%% under. So it leans Under for %s." % (base, -pct, span)
    else:
        txt = "%s is on par with the pre-game line, so there is no scoring lean for %s." % (base, span)
    mu = it.get("mu") or 0.0
    name = None
    if mu >= 2.0:
        name = it["home"]
    elif mu <= -2.0:
        name = it["away"]
    txt += (" %s is the stronger side on the pre-game line." % name) if name else " No clearly stronger side on the pre-game line."
    return txt


def format_outlook(items, limit=8):
    """Telegram HTML block for /quarters and the hourly summary."""
    e = html.escape
    if not items:
        return "No live game with a usable clock right now."
    L = []
    for it in items[:limit]:
        where = "Half-time" if it["halftime"] else ("%s %s" % (_label(it.get("n", 4), it["period"]),
                                                                it.get("clock", "")))
        L.append("\U0001F3C0 <b>%s vs %s</b> | %s | %d-%d" % (e(str(it["home"])), e(str(it["away"])), where.strip(),
                                                          it["home_score"], it["away_score"]))
        L.append("<i>%s</i>" % e(reading(it)))
        for q in it["quarters"]:
            bits = ["~%d pts" % round(q["exp_pts"])]
            if q["total_lean"]:
                bits.append("pace %+d%% (%s)" % (round(100 * (it["ratio"] - 1)), q["total_lean"]))
            L.append("  <b>%s</b>: %s" % (q["label"], " | ".join(bits)))
        if it.get("source") != "pinnacle":
            L.append("  <i>league averages (no Pinnacle line for this game)</i>")
        L.append("")
    if len(items) > limit:
        L.append("+%d more live game(s)" % (len(items) - limit))
    L.append("<i>FORECAST ONLY: built from the pre-game line and live pace. No price is checked here, so these "
             "are not bets. A priced bet is sent as its own alert when it passes the rules.</i>")
    return "\n".join(L)


def sent_phases() -> set:
    """Phases that get a Telegram message. Default '1,3': Q1 (Q1, Q2, 1st half) and Q3 (Q3, Q4, 2nd half) carry everything
    the Q2 / Q4 messages repeat. QUARTER_PHASES=1,2,3,4 gives every quarter back."""
    import os
    raw = os.environ.get("QUARTER_PHASES", "1,3")
    return {x.strip() for x in raw.split(",") if x.strip()}


def phase_of(it):
    """Which phase a game is in, for the once-per-phase message: 1, 2, 'HT', 3 or 4 (four-period games only)."""
    if it.get("n", 4) != 4:
        return None
    if it.get("halftime"):
        done = it.get("break_after") or it.get("n", 4) // 2
        return "HT" if done == it.get("n", 4) // 2 else "B%d" % done
    return it.get("period") if it.get("period") in (1, 2, 3, 4) else None


def _dots(rows):
    """[(label, text)] -> aligned lines 'Label ....: text' (monospace block, so the columns line up in Telegram)."""
    w = max(len(l) for l, _ in rows) + 1
    return "\n".join("%s%s: %s" % (l, " " + "." * (w - len(l)) if w - len(l) > 0 else "", v) for l, v in rows)


def phase_message(it, ht_total=None, q3_total=None, q1_total=None):
    """One message when a live game ENTERS a quarter (never at half-time): a FORECAST from the pre-game line and the live
    pace, never a bet. Q1: Q1, Q2, 1st half. Q2: Q2, 1st half. Q3: Q3, Q4, 2nd half. Q4: Q4, 2nd half.
    A half total = points already scored in the half + the rest of the current quarter + the next quarter.
    The feed gives only the RUNNING total, so a split of the points already played between two quarters is not shown
    ('played so far' in Q2 / Q4); the 2nd-half total needs the half-time score (ht_total), without it only the rest is shown."""
    ph = phase_of(it)
    if ph is None or isinstance(ph, str):
        return None
    e = html.escape
    qs = {x["label"].replace(" (rest)", ""): x for x in it["quarters"]}
    cur = it["home_score"] + it["away_score"]
    pts = lambda lab: round(qs[lab]["exp_pts"]) if lab in qs else None
    rows = []
    if ph == 1:
        a, b = pts("Q1"), pts("Q2")
        if a is None or b is None:
            return None
        rows = [("Earlier of Q1", "%d Points" % cur), ("Rest of Q1", "%d Points" % a), ("Q2 Forecast", "%d Points" % b),
                ("1st half Forecast", "%d Points" % (cur + a + b))]
    elif ph == 2:
        a = pts("Q2")
        if a is None:
            return None
        if q1_total is not None and q1_total <= cur:
            early = cur - q1_total
            rows = [("Q1 Break", "%d Points" % q1_total), ("Earlier Q2", "%d Points" % early),
                    ("Rest of Q2", "%d Points" % a), ("1st Half", "%d Points" % (q1_total + early + a))]
        else:
            rows = [("Played so far", "%d Points" % cur), ("Rest of Q2", "%d Points" % a), ("1st Half", "%d Points" % (cur + a))]
    elif ph == 3:
        a, b = pts("Q3"), pts("Q4")
        if a is None or b is None:
            return None
        if ht_total is not None:
            early = max(0, cur - ht_total)
            rows = [("Earlier of Q3", "%d Points" % early), ("Rest of Q3", "%d Points" % a), ("Q4 Forecast", "%d Points" % b),
                    ("2nd Half Forecast", "%d Points" % (early + a + b))]
        else:
            rows = [("Rest of Q3", "%d Points" % a), ("Q4 Forecast", "%d Points" % b), ("Rest of 2nd Half", "%d Points" % (a + b))]
    else:
        a = pts("Q4")
        if a is None:
            return None
        if ht_total is not None and q3_total is not None and ht_total <= q3_total <= cur:
            early = cur - q3_total                          # Q4 so far
            q3 = q3_total - ht_total                        # finished Q3
            rows = [("Q3 Break", "%d Points" % q3), ("Earlier Q4", "%d Points" % early),
                    ("Rest of Q4", "%d Points" % a), ("2nd Half", "%d Points" % (q3 + early + a))]
        elif ht_total is not None:
            early = max(0, cur - ht_total)
            rows = [("2nd Half so far", "%d Points" % early), ("Rest of Q4", "%d Points" % a), ("2nd Half", "%d Points" % (early + a))]
        else:
            rows = [("Rest of Q4", "%d Points" % a)]
    lines = _situation_room(it, rows)
    return "\n".join(lines)


def _half(x):
    """Round to the nearest .5 (a bookmaker-style line)."""
    return round(x * 2) / 2.0


def _situation_room(it, rows):
    """Telegram lines for one phase message. 'Market Situation Room' = the model's three reads: who is favoured for the
    game, which way the half total leans against the PRE-GAME half pace (T/2), and the expected game margin as a spread.
    These are model reads, not bookmaker lines: no price is checked."""
    e = html.escape
    ph = phase_of(it)
    ratio, home, away = it["ratio"], str(it["home"]), str(it["away"])
    mu, rem, diff, T = it.get("mu") or 0.0, it.get("rem") or 0.0, it.get("diff") or 0.0, it.get("T") or 0.0
    exp_margin = diff + mu * rem                   # expected final margin (home minus away)
    if exp_margin >= 2.0:
        fav, spread = "%s favoured" % e(home), "Spread %s %s" % (e(home), "%+.1f" % -_half(abs(exp_margin)))
    elif exp_margin <= -2.0:
        fav, spread = "%s favoured" % e(away), "Spread %s %s" % (e(away), "%+.1f" % -_half(abs(exp_margin)))
    else:
        fav, spread = "No clear favourite", "Spread: pick'em"
    ref = _half(T / 2.0) if T else None
    half_name = "1st half" if ph in (1, 2) else "2nd half"
    if ref is not None and ratio >= 1 + TOTAL_LEAN:
        total = "Over %.1f (%s, pre-game pace)" % (ref, half_name)
    elif ref is not None and ratio <= 1 - TOTAL_LEAN:
        total = "Under %.1f (%s, pre-game pace)" % (ref, half_name)
    else:
        total = "No scoring lean (%s)" % half_name
    where = "Q%d %s left" % (ph, it.get("clock", "")) if it.get("clock") else "Q%d" % ph
    out = ["\U0001F4C8 <b>QUARTER OUTLOOK</b> \u00B7 %s" % e(where.strip()), "\u2501" * 15,
           "%s: %d" % (e(home), it["home_score"]), "%s: %d" % (e(away), it["away_score"]), "",
           "<b>Market Situation Room</b>", "1. %s" % fav, "2. %s" % total, "3. %s" % spread, "",
           "<b>EXPECTED POINTS</b>", "<pre>%s</pre>" % e(_dots(rows)), ""]
    src = it.get("source")
    if src == "pinnacle_spread":
        out.append("\u2139\uFE0F Pinnacle spread used; scoring pace from league average (no Pinnacle total for this game).")
    elif src == "pinnacle_total":
        out.append("\u2139\uFE0F Pinnacle total used; strength from league average (no Pinnacle spread for this game).")
    elif src != "pinnacle":
        out.append("\u2139\uFE0F League averages used (no Pinnacle line for this game).")
    out += ["\u2139\uFE0F <b>Forecast Only.</b>", "No bookmaker price is checked, so this is not a bet."]
    return out
