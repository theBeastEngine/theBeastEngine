"""All message formatting lives in format_pick(): Prematch / Live / Sim alert templates."""
import html
import os
from datetime import datetime, timedelta, timezone
from config.settings import settings, SCAN_SECONDS
from services.client import post_json, HttpError
from services.logger import get_logger

log = get_logger("telegram")
API = "https://api.telegram.org/bot{token}/{method}"
LABELS = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}
_START_KEYS = ("commence_time", "start_time", "start", "commence", "kickoff")


def api(method: str, **payload):
    if not settings.TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    return post_json(API.format(token=settings.TELEGRAM_TOKEN, method=method), payload)


PERIODS = {"full": "", "h1": " (1st half)", "q1": " (Q1)", "q2": " (Q2)", "q3": " (Q3)", "q4": " (Q4)", "h2": " (2nd half)"}


def _mlabel(p: dict) -> str:
    from core.gates import kind_period
    kind, per = kind_period(p)
    ot = " incl. OT" if str(p.get("market", "")).endswith("ot") and per in ("q4", "h2") else ""
    return {"h2h": "Moneyline", "spread": "Spread", "total": "Total"}[kind] + PERIODS.get(per, "") + ot


def describe(p: dict) -> str:
    from core.gates import kind_period
    m = {"h2h": "h2h", "spread": "spreads", "total": "totals"}[kind_period(p)[0]]
    sel, line = p["selection"], p.get("line")
    team = p["home"] if sel == "home" else p["away"]
    if m == "h2h":
        return f"{team} to win"
    if m == "spreads":
        return f"{team} {(line if sel == 'home' else -line):+g}"
    return f"{sel.title()} {line:g}"


def _tz_offset() -> float:
    try:
        return float(os.environ.get("ALERT_TZ_OFFSET_HOURS", "1"))  # default WAT (UTC+1)
    except ValueError:
        return 1.0


def _start_text(p: dict) -> str:
    for k in _START_KEYS:
        v = p.get(k)
        if v in (None, ""):
            continue
        try:
            if isinstance(v, datetime):
                dt = v
            elif isinstance(v, (int, float)):
                dt = datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except (ValueError, OverflowError, OSError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # engine times are naive UTC
        off = _tz_offset()
        dt = dt.astimezone(timezone(timedelta(hours=off)))
        label = "WAT" if off == 1 else "UTC%+g" % off
        return f"{dt:%d %b %H:%M} {label}"
    return ""


def _clock_text(p: dict) -> str:
    c = p.get("clock")
    if c not in (None, ""):
        return str(c)
    for k in ("remaining_sec", "remaining", "time_left"):
        v = p.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            s = max(int(v), 0)
            return f"{s // 60}:{s % 60:02d}"
    return ""


RULE = "\u2501" * 15
KINDS = {"live": "Live Match", "prematch": "Pre-Match", "sim": "Simulation"}
HEADS = {"live": "\U0001F534 <b>LIVE VALUE</b>", "prematch": "\U0001F3C0 <b>PREMATCH VALUE</b>",
         "sim": "\U0001F3B2 <b>SIM VALUE</b>"}


def _prob_dot(pr) -> str:
    """Quick visual of how likely the bet is to win: green 65%+, yellow 58%+, orange below."""
    return "\u2705" if pr >= 0.65 else ("\U0001F536" if pr >= 0.58 else "\U0001F538")


def tier(p: dict) -> str:
    """A / B / C from the win probability, one step lower when the price is not backed by a real Pinnacle line."""
    pr = p.get("p") or 0.0
    t = 0 if pr >= 0.65 else (1 if pr >= 0.58 else 2)
    if p.get("pin_real") is False:
        t = min(2, t + 1)
    return "ABC"[t]


def format_position_alert(a: dict) -> str:
    """Urgent notice for ONE pre-match market whose Pinnacle price moved. The other markets of the game are unchanged."""
    e = html.escape
    v, st = a["pos"], a["status"]
    pick = dict(home=v.get("home"), away=v.get("away"), selection=v.get("sel"), line=v.get("line"), market=v.get("market"))
    head = {"exit": "\U0001F6A8\U0001F6A8 <b>URGENT: PINNACLE MOVED</b> \U0001F6A8\U0001F6A8",
            "switch": "\U0001F6A8\U0001F6A8 <b>URGENT: SWITCH SIDE</b> \U0001F6A8\U0001F6A8",
            "watch": "\u26A0\uFE0F <b>WATCH: PRICE SLIPPING</b>"}[st]
    try:
        what = _mlabel(pick) + " " + describe(pick)
    except Exception:
        what = "%s %s" % (v.get("market"), v.get("sel"))
    lines = [head, RULE, "\U0001F3C6 <b>%s</b>" % e(str(v.get("league", "")).upper()),
             "\u2694\uFE0F %s vs %s" % (e(str(v.get("home"))), e(str(v.get("away")))), "",
             "\U0001F4CC Your bet (#%s): <b>%s</b> @ %.2f" % (v.get("id"), e(what), float(v.get("odds") or 0)),
             "\U0001F4C9 What changed: <b>%s</b>" % e(a.get("detail", ""))]
    if st == "exit":
        lines += ["", "\u27A1\uFE0F <b>Action: this bet has lost its edge. Do not add to it; leaving it is your call.</b>"]
    elif st == "switch":
        n = a.get("new") or {}
        lines += ["", "\u27A1\uFE0F <b>Action: the other side now qualifies. It follows as a normal alert with its own buttons.</b>"]
        if n:
            lines.append("\U0001F449 New side: <b>%s</b>" % e(_mlabel(n) + " " + describe(n)))
    else:
        lines += ["", "<i>Still positive, but thinner than when it was sent. No action needed unless it keeps sliding.</i>"]
    lines += ["", "<i>Only this market changed. Your other markets for this game are unchanged.</i>"]
    return "\n".join(lines)


def format_pick(p: dict) -> str:
    """Telegram HTML alert. The things you act on (bet, price, minimum odds, EV, win probability, stake) are bold."""
    e = html.escape
    mode = p.get("mode")
    pid = f"  #{p['id']}" if p.get("id") is not None else ""
    head = HEADS.get(mode, "<b>VALUE</b>") + pid
    if p.get("reversal"):
        head = "\U0001F6A8\U0001F6A8 <b>REVERSAL</b> \U0001F6A8\U0001F6A8\n" + head
    elif p.get("switch_note"):
        head = "\U0001F501 <b>SWITCH</b>\n" + head
    league = e(str(p["league"]).upper())
    teams = f"{e(p['home'])} vs {e(p['away'])}"
    if mode == "live":
        bits = [f"\U0001F3C6 <b>{league}</b>"]
        if p.get("period"):
            unit = "H" if p["league"] == "ncaab" else "Q"
            clock = _clock_text(p)
            bits.append(f"\u23F1 <b>{unit}{p['period']}</b>" + (f" {clock} left" if clock else ""))
        h, a = p.get("home_score"), p.get("away_score")
        score = f"{e(p['home'])} <b>{h} - {a}</b> {e(p['away'])}" if h is not None and a is not None else teams
        lines = [head, RULE, "  \u00B7  ".join(bits), f"\u2694\uFE0F {score}"]
    else:
        start = _start_text(p)
        lines = [head, RULE, f"\U0001F3C6 <b>{league}</b>" + (f"  \u00B7  \U0001F550 {e(start)}" if start else ""),
                 f"\u2694\uFE0F {teams}"]
    lines += ["", f"\U0001F3AF <b>BET: {e(_mlabel(p) + ' ' + describe(p))}</b>",
              f"\U0001F4B5 {e(str(p['book']))} @ <b>{p['odds']:.2f}</b>   (fair {p['fair_odds']:.2f})",
              f"\u2705 Min odds to take: <b>{p['min_odds']:.2f}</b>", ""]
    stats = f"\U0001F4C8 EV: <b>{p['ev'] * 100:+.1f}%</b>"
    if p.get("p") is not None:
        stats += f"   {_prob_dot(p['p'])} Win prob: <b>{p['p'] * 100:.1f}%</b>"
        if p.get("p_model") is not None:
            stats += f" (model {p['p_model'] * 100:.0f}%)"
    lines += [stats, f"\U0001F4B0 Stake: <b>\u20a6{int(round(p['stake'])):,}</b>", ""]
    if p.get("reversal"):
        lines.append("\U0001F6A8 <b>Why the call changed:</b> " + e(str(p["reversal"])))
    if p.get("switch_note"):
        lines.append("\U0001F501 " + e(str(p["switch_note"])))
    if p.get("stance"):
        lines.append("\U0001F512 Stance on this game: <b>" + e(str(p["stance"])) + "</b>")
    if p.get("p") is not None:
        lines.append("\U0001F3C5 Tier: <b>%s</b>" % tier(p))
    lines.append(f"\U0001F3F7 Type of Match: <b>{KINDS.get(mode, '-')}</b>")
    pin = p.get("pin_real")
    if pin is not None:
        lines.append("\U0001F4CC League on Pinnacle: " + ("<b>Yes</b> \u2705" + (" (pre-game lines)" if mode == "live" else "") if pin else "<b>No</b> (model priors only) \u26A0\uFE0F"))
    if p.get("priority"):
        lines.append(f"\u2B50 Priority: <b>{e(str(p['priority']))}</b>")
    if mode == "sim":
        lines += ["", "<i>\u26A0\uFE0F Model-based estimate: check the line before you place it.</i>"]
    if mode == "live":
        if p.get("pin_live"):
            lines += ["\U0001F4E1 <b>PINNACLE LIVE</b>: fair price checked against Pinnacle's current price"]
        else:
            lines += ["\U0001F9EE <b>MODEL ONLY</b>: fair price is the model's, built from the pre-game Pinnacle line (no live Pinnacle price was found)"]
    if mode == "prematch":
        lines += ["\U0001F4CA Fair price: Pinnacle line (vig removed)"]
    if mode == "live":
        lines += ["", f"\u23F3 Valid for ~{p.get('ttl_min') or max(1, round(SCAN_SECONDS / 60))} min"]
    return "\n".join(lines)


# ---- one message per GAME: its best markets (Prematch up to 5, Sim / Live up to 3), ranked -------------------------------
_PER_NAME = {"q1": "1st quarter", "q2": "2nd quarter", "q3": "3rd quarter", "q4": "4th quarter",
             "h1": "1st half", "h2": "2nd half"}
_NUM = ["1\uFE0F\u20E3", "2\uFE0F\u20E3", "3\uFE0F\u20E3", "4\uFE0F\u20E3", "5\uFE0F\u20E3"]
_PRIO_RANK = {"High": 0, "Medium": 1, "Standard": 2}


def _scope(p: dict) -> str:
    from core.gates import kind_period
    _kind, per = kind_period(p)
    ot = str(p.get("market", "")).endswith("ot") and per in ("q4", "h2")
    if per == "full":
        return "in the game, overtime included"
    return "in the " + _PER_NAME.get(per, per) + (", overtime included" if ot else "")


def _n(x) -> str:
    return ("%d" % x) if float(x).is_integer() else ("%g" % x)


def narrate(p: dict) -> str:
    """One plain-English sentence: what has to happen for this bet to win. Built only from the pick's own line and side."""
    from core.gates import kind_period
    kind, per = kind_period(p)
    sc = _scope(p)
    sel, line = p.get("selection"), p.get("line")
    team = p.get("home") if sel == "home" else p.get("away")
    if kind == "h2h":
        what = "win the game, overtime included" if per == "full" else "win the " + _PER_NAME.get(per, per)
        return "%s just need to %s, by any margin." % (team, what) if team else "Needs to win."
    if line is None:
        return ""
    line = float(line)
    if kind == "spread":
        lt = line if sel == "home" else -line
        if lt == 0:
            return "%s must win %s; a tie is a refund." % (team, sc)
        m = abs(lt)
        if lt > 0:
            if not m.is_integer():
                k = int(m)
                if k == 0:
                    return "%s must win %s." % (team, sc)
                return "%s can lose by %d point%s or fewer, or win, %s." % (team, k, "" if k == 1 else "s", sc)
            return "%s can lose by fewer than %s points, or win, %s (losing by exactly %s is a refund)." % (team, _n(m), sc, _n(m))
        if not m.is_integer():
            k = int(m) + 1
            return "%s must win by %d points or more %s." % (team, k, sc)
        return "%s must win by more than %s points %s (winning by exactly %s is a refund)." % (team, _n(m), sc, _n(m))
    if kind == "total":
        if sel == "over":
            if not line.is_integer():
                return "The two teams score %d or more points combined %s." % (int(line) + 1, sc)
            return "The two teams score more than %s points combined %s (exactly %s is a refund)." % (_n(line), sc, _n(line))
        if not line.is_integer():
            return "The two teams score %d points or fewer combined %s." % (int(line), sc)
        return "The two teams score fewer than %s points combined %s (exactly %s is a refund)." % (_n(line), sc, _n(line))
    return ""


def game_order(p: dict):
    """Priority first (Total > Spread > Moneyline), then Tier A > B > C, then the bigger edge."""
    from core.gates import priority_name
    return (_PRIO_RANK.get(priority_name(p), 3), "ABC".index(tier(p)) if p.get("p") is not None else 2,
            -(p.get("ev") or 0.0))


def _cap_pct() -> float:
    try:
        from core.kelly import match_exposure_pct
        return match_exposure_pct()
    except Exception:
        return 0.0


def format_game_alert(picks: list, bankroll: float = None) -> str:
    """ONE Telegram HTML message for one game: all of its qualifying markets, best first. Every pick keeps its own id."""
    e = html.escape
    rows = sorted(picks, key=game_order)
    first = rows[0]
    mode = first.get("mode")
    n = len(rows)
    badge = {"live": "\U0001F534 <b>LIVE VALUE", "sim": "\U0001F3B2 <b>SIM VALUE"}.get(mode, "\U0001F3C0 <b>PREMATCH VALUE")
    lines = []
    if any(r.get("reversal") for r in rows):
        lines.append("\U0001F6A8\U0001F6A8 <b>REVERSAL</b> \U0001F6A8\U0001F6A8")
    elif any(r.get("switch_note") for r in rows):
        lines.append("\U0001F501 <b>SWITCH</b>")
    lines += ["%s \u00B7 %d pick%s</b>" % (badge, n, "" if n == 1 else "s"), RULE]
    league = e(str(first["league"]).upper())
    if mode == "live":
        bits = ["\U0001F3C6 <b>%s</b>" % league]
        if first.get("period"):
            unit = "H" if first["league"] == "ncaab" else "Q"
            clock = _clock_text(first)
            bits.append("\u23F1 <b>%s%s</b>" % (unit, first["period"]) + (" %s left" % clock if clock else ""))
        lines.append(" \u00B7 ".join(bits))
        h, a = first.get("home_score"), first.get("away_score")
        if h is not None and a is not None:
            lines.append("\u2694\uFE0F %s <b>%s \u2013 %s</b> %s" % (e(first["home"]), h, a, e(first["away"])))
        else:
            lines.append("\u2694\uFE0F %s vs %s" % (e(first["home"]), e(first["away"])))
        if first.get("stance"):
            lines.append("\U0001F512 Stance on this game: <b>%s</b>" % e(str(first["stance"])))
    else:
        start = _start_text(first)
        lines.append("\U0001F3C6 <b>%s</b>" % league + ("  \u00B7  \U0001F550 %s" % e(start) if start else ""))
        lines.append("\u2694\uFE0F %s vs %s" % (e(first["home"]), e(first["away"])))
        if mode == "prematch":
            lines.append("\U0001F4CA Pinnacle fair prices (vig removed) \u2705" if first.get("pin_real") is not False
                         else "\u26A0\uFE0F No Pinnacle line for this league: model priors only")
    note = first.get("rest_note")
    if note and mode == "prematch":
        lines.append("\U0001F6CC " + e(str(note)))
    inj = first.get("injury_note")
    if inj and mode == "prematch":
        lines.append("\U0001FA7A " + e(str(inj)))
    total = 0
    for i, p in enumerate(rows):
        from core.gates import priority_name
        pr = priority_name(p)
        star = "\u2B50 " if pr in ("High", "Medium") else ""
        lines += ["", "%s %s<b>%s</b> \u00B7 %s" % (_NUM[i] if i < len(_NUM) else "%d." % (i + 1), star, pr.upper(), e(_mlabel(p))),
                  "\U0001F3AF <b>%s</b> @ <b>%.2f</b> (fair %.2f)" % (e(describe(p)), p["odds"], p["fair_odds"])]
        nar = narrate(p)
        if nar:
            lines.append("\U0001F4AC " + e(nar))
        stats = "\u2705 Min odds <b>%.2f</b> \u00B7 \U0001F4C8 EV <b>%+.1f%%</b>" % (p["min_odds"], p["ev"] * 100)
        if p.get("p") is not None:
            stats += " \u00B7 %s Win <b>%.0f%%</b> \u00B7 Tier <b>%s</b>" % (_prob_dot(p["p"]), p["p"] * 100, tier(p))
        lines.append(stats)
        tag = "  [#%s]" % p["id"] if p.get("id") is not None else ""
        lines.append("\U0001F4B0 Stake <b>\u20a6%s</b>%s" % (format(int(round(p["stake"])), ","), tag))
        total += p["stake"]
        if p.get("reversal"):
            lines.append("\U0001F6A8 <b>Why the call changed:</b> " + e(str(p["reversal"])))
        if p.get("switch_note"):
            lines.append("\U0001F501 " + e(str(p["switch_note"])))
    cap = _cap_pct()
    if mode != "live" or n > 1:
        foot = "\U0001F4BC On this game: <b>\u20a6%s</b>" % format(int(round(total)), ",")
        if bankroll and cap > 0:
            foot += " (cap %g%% = \u20a6%s)" % (round(cap * 100, 1), format(int(round(bankroll * cap)), ","))
        lines += ["", foot]
        if any(r.get("stake_scaled") for r in rows):
            lines.append("<i>Stakes were scaled down to stay inside the per-game cap.</i>")
    if mode == "live":
        if rows and all(r.get("pin_live") for r in rows):
            tag = "\U0001F4E1 <b>PINNACLE LIVE</b>: the fair price was checked against Pinnacle's current price."
        else:
            tag = "\U0001F9EE <b>MODEL ONLY</b>: the fair price is the model's, built from the pre-game Pinnacle line (no live Pinnacle price was found)."
        lines += ["", tag, "\u23F3 Valid for about %s min" % (first.get("ttl_min") or max(1, round(SCAN_SECONDS / 60)))]
    elif mode == "sim":
        lines += ["", "<i>\u26A0\uFE0F Model estimate from simulated games. Check the line before you place it.</i>"]
    lines += ["", "<i>Tap a number below once you have placed that bet.</i>"]
    return "\n".join(lines)


def ids_keyboard(ids: list, placed: set = None) -> dict:
    """Numbered buttons in the given order; a tap toggles 'placed' for that pick (callback carries the whole id list so the
    row can be redrawn with a tick on the placed ones)."""
    csv = ",".join(str(i) for i in ids)
    placed = placed or set()
    btn = [{"text": ("\u2705%d" if pid in placed else "%d") % (i + 1), "callback_data": "tog:%s:%s" % (pid, csv)}
           for i, pid in enumerate(ids)]
    return {"inline_keyboard": [btn]} if btn else {"inline_keyboard": []}


def game_keyboard(picks: list, placed: set = None) -> dict:
    rows = sorted(picks, key=game_order)
    return ids_keyboard([p["id"] for p in rows if p.get("id") is not None], placed)


def notify_game(picks: list) -> bool:
    bankroll = None
    try:
        from storage import repository as repo
        bankroll = repo.bankroll_current()
    except Exception:
        pass
    return send(format_game_alert(picks, bankroll), game_keyboard(picks))


def keyboard(pick_id: int, stage: str = "new") -> dict:
    if stage == "new":
        row = [("PLACE", f"place:{pick_id}"), ("SKIP", f"skip:{pick_id}")]
    else:
        row = [("PLACED", f"placed:{pick_id}"), ("SKIP", f"skip:{pick_id}")]
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]]}


def send(text: str, markup: dict = None, chat_id: str = None, silent: bool = False) -> bool:
    """Returns True only if every chunk was accepted by Telegram. silent=True: no sound / vibration (hourly summary)."""
    chat_id = chat_id or settings.TELEGRAM_CHAT_ID
    if not chat_id:
        log.warning("TELEGRAM_CHAT_ID not set; message dropped")
        return False
    ok = True
    for i in range(0, len(text), 3900):
        payload = dict(chat_id=chat_id, text=text[i:i + 3900], parse_mode="HTML")
        if silent:
            payload["disable_notification"] = True
        if markup and i + 3900 >= len(text):
            payload["reply_markup"] = markup
        try:
            api("sendMessage", **payload)
        except HttpError as e:
            log.error("send failed: %s", e)
            ok = False
    return ok


def notify(pick: dict) -> bool:
    return send(format_pick(pick), keyboard(pick["id"]))
