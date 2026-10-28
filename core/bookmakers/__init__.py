"""Row helpers + adapter loader for native soft-book scrapers.
Row = {match_id, market, side, odds, line, book} (+ league, home, away, commence for prematch).
Spread rows: `line` is ALWAYS the HOME line. Totals: side over/under + the total line. h2h: line None."""
import importlib

BOOKS = ("sportybet", "bet9ja", "betano", "1xbet", "betking", "msport", "nairabet", "paripesa")

# module file name when it differs from the book name (a file can't start with a digit safely)
_MODULE = {"1xbet": "xbet"}

_SIDES = {"h2h": ("home", "away"), "spreads": ("home", "away"), "totals": ("over", "under"),
          "ML": ("home", "away"), "Spread": ("home", "away"), "Total": ("over", "under")}
for _p in ("q1", "q2", "q3", "q4", "h1", "h2", "q4ot", "h2ot"):   # period rows: "Spread_q1" / "Total_h1" ... (canonical names spreads_q1 ...)
    _SIDES["Spread_" + _p] = ("home", "away")
    _SIDES["Total_" + _p] = ("over", "under")
    _SIDES["spreads_" + _p] = ("home", "away")
    _SIDES["totals_" + _p] = ("over", "under")


def make_row(match_id, market, side, odds, line, book, **extra):
    try:
        odds = float(odds)
        line = None if line is None else float(line)
    except (TypeError, ValueError):
        return None
    if market not in _SIDES or side not in _SIDES[market] or not book or not (1.01 < odds < 50):
        return None
    if market in ("h2h", "ML"):
        line = None
    elif line is None:
        return None
    row = dict(match_id=None if match_id is None else str(match_id), market=market, side=side,
               odds=odds, line=line, book=str(book))
    for k, v in extra.items():
        if v is not None and k not in row:
            row[k] = v
    return row


def clean_rows(rows):
    out = []
    for r in rows or []:
        x = make_row(r.get("match_id"), r.get("market"), r.get("side"), r.get("odds"), r.get("line"), r.get("book"))
        if x:
            out.append({**r, **x})
    return out


def get_adapter(book: str):
    """core/bookmakers/<module>.py, or None when no adapter file exists."""
    for name in (_MODULE.get(book, book), book, "b_" + book):
        try:
            return importlib.import_module(f"core.bookmakers.{name}")
        except ModuleNotFoundError as e:
            if e.name and e.name.startswith("core.bookmakers"):
                continue
            raise
    return None
