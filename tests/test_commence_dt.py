from datetime import datetime
from utils.dates import commence_dt

def test_commence_dt_forms():
    assert commence_dt(None) is None and commence_dt("") is None and commence_dt("junk") is None
    assert commence_dt("2026-10-07T01:30:00Z") == datetime(2026, 10, 7, 1, 30)
    assert commence_dt("2026-10-07T02:30:00+01:00") == datetime(2026, 10, 7, 1, 30)
    assert commence_dt(1791336600) == datetime(2026, 10, 7, 1, 30)
    assert commence_dt(1791336600000) == datetime(2026, 10, 7, 1, 30)
    dt = datetime(2026, 10, 7, 1, 30)
    assert commence_dt(dt) == dt
