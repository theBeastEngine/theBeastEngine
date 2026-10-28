from storage import repository as repo
from storage.db import init_db

if __name__ == "__main__":
    init_db()
    print("backed up", repo.backup_pinnacle(), "pinnacle rows")
