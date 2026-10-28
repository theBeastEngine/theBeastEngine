import os
import tempfile

# must be set before config.settings is imported
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="beast_test_"), "test.db")

import pytest

os.environ.setdefault("TRACKER", "0")   # the Match Tracker has its own tests; the older pre-match tests expect per-scan alerts


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    from storage.db import init_db
    init_db()
    yield


@pytest.fixture(autouse=True)
def _fresh_odds_key_book():
    """The Odds API key book is shared by the whole process; every test starts with every key alive."""
    from services import odds_api
    bk = odds_api.book()
    bk.load = bk.save = None
    bk._d = None
    yield
    bk._d = None
