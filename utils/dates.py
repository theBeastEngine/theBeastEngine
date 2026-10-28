import re
from datetime import date, datetime, timedelta
from utils.helpers import utcnow

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _month(s):
    return MONTHS.get(s[:3]) if s else None


def parse_range(text: str, today: date = None):
    """'3rd to 17th October 2026' | '3 oct to 17 oct 2026' | '28 dec to 3 jan' | '5 oct' | '' (today).
    Returns (start, end_exclusive) as naive datetimes. Raises ValueError on garbage."""
    today = today or utcnow().date()
    t = text.lower().strip()
    if t in ("", "today"):
        a = b = today
    else:
        m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s*([a-z]{3,9})?\s*(?:to|until|-|\u2013)\s*"
                      r"(\d{1,2})(?:st|nd|rd|th)?\s*([a-z]{3,9})?\s*(\d{4})?", t)
        if m:
            d1, m1, d2, m2, y = m.groups()
            mon2 = _month(m2) or _month(m1)
            mon1 = _month(m1) or mon2
            if not mon2:
                raise ValueError("month missing")
            year_given = bool(y)
            y = int(y) if y else today.year
            a, b = date(y, mon1, int(d1)), date(y, mon2, int(d2))
            if b < a and mon2 < mon1:
                if year_given:
                    a = date(y - 1, mon1, int(d1))      # '28 dec to 3 jan 2027'
                else:
                    b = date(y + 1, mon2, int(d2))      # '28 dec to 3 jan' crosses the new year
        else:
            m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s*([a-z]{3,9})\s*(\d{4})?", t)
            if not m or not _month(m.group(2)):
                raise ValueError("unrecognised date")
            y = int(m.group(3)) if m.group(3) else today.year
            a = b = date(y, _month(m.group(2)), int(m.group(1)))
    if b < a:
        a, b = b, a
    return datetime(a.year, a.month, a.day), datetime(b.year, b.month, b.day) + timedelta(days=1)


def commence_dt(v):
    """Feed start time (datetime, ISO text or epoch) -> naive UTC datetime, or None."""
    from datetime import datetime, timezone
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        x = v
    elif isinstance(v, (int, float)):
        x = datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    else:
        try:
            x = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if x.tzinfo:
        x = x.astimezone(timezone.utc).replace(tzinfo=None)
    return x
