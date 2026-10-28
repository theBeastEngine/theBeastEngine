# scripts/smoke_adapters.py  -- python -m scripts.smoke_adapters [league_id]
import asyncio
import sys

from core.bookmakers import get_adapter
from config.settings import SOFT_BOOKS

TIMEOUT_SEC = 60


async def main(league="nba") -> int:
    ok = 0
    for book in SOFT_BOOKS:
        a = get_adapter(book)
        if a is None:
            print(f"{book:10s} FAIL  adapter did not load")
            continue
        try:
            rows = await asyncio.wait_for(a.fetch_odds(league), TIMEOUT_SEC)
        except asyncio.TimeoutError:
            print(f"{book:10s} FAIL  timeout after {TIMEOUT_SEC}s")
            continue
        except Exception as e:
            print(f"{book:10s} FAIL  {e!r}")
            continue
        games = len({r.get("match_id") for r in rows})
        ok += bool(rows)
        print(f"{book:10s} {'OK  ' if rows else 'EMPTY'} rows={len(rows)} games={games}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "nba")))
