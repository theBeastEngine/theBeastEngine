"""Worker entry for Render (background worker): same as bot.py."""
import runpy

if __name__ == "__main__":
    runpy.run_path("bot.py", run_name="__main__")
