import core.beast as beast
from utils.dates import commence_dt

def test_beast_uses_commence_dt():
    assert beast.commence_dt is commence_dt
