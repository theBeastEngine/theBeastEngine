"""Paid one-off gold build (~3 credits per league with an Odds API key; ~42 for a full sweep).
Usage: python -m scripts.build_gold --yes"""
import sys
from core.beast import Beast
from storage.db import init_db

if __name__ == "__main__":
    if "--yes" not in sys.argv:
        sys.exit("This spends Odds API credits. Re-run with --yes.")
    init_db()
    b = Beast()
    b.ensure_gold()
    print(b.build_gold())
