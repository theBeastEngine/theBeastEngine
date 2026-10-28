# TheBEAST Basketball Engine

Basketball value-bet engine. It compares soft-book prices against Pinnacle fair prices, finds edges, and sends picks to Telegram for manual approval. Nothing is bet automatically.

## What it does
- Prematch: compares SportyBet against the real Pinnacle price of the same game (Odds API, cached on disk). The Pinnacle golden table (14 leagues x 15 markets x 9 team tiers = 1890 rows) stays as the fallback and the team-tier structure.
- Live: scans every 2 minutes from SportyBet's live feed (score, clock and live lines) at 0 Odds API credits. Games in a break, in the first or last minutes, or in a blowout are not alerted.
- Devigs the reference prices, adjusts them to each book's line, and keeps edges of at least 4%. Edges above 20% are treated as bad lines and skipped.
- Prematch alert = 5 best EVs. Scheduled Live alert = 3 best EVs. Live started with /live in Telegram = the single best EV. Simulation = top 3.
- Stakes a flat amount, capped at 5% of the current bankroll and rounded to the nearest 100.
- Sends picks to Telegram. A pick is only remembered once it has actually been sent.

The engine settings above are fixed in `config/settings.py`, and `tests/test_locks.py` checks them. They are not configurable from `.env` on purpose.

## Layout
- `api/` FastAPI app (`/health` is static, so Render's health check never depends on the DB or any API)
- `core/` engine, math, scanner, scheduler
- `core/bookmakers/` Pinnacle gold-table builder, soft-book feed, and one adapter per Nigerian book
- `services/` Odds API client, scraper bridge, ratings, logging
- `storage/` SQLAlchemy models and repository (SQLite)
- `data/team_tiers.json` real team tiers per league
- `bot.py` Telegram bot, `main.py` web entry point
- `tests/` offline tests (run them before every push)

## Run locally
    pip install -r requirements-dev.txt
    python -m pytest -q
    uvicorn api.server:app --port 8000

Copy `.env.example` to `.env` and fill in the values you need. Never commit `.env`.

## Deploy on Render
`render.yaml` creates a web service with a 1 GB disk mounted at `/data`. The database, the gold-table backup and the scraper captures all live there so they survive redeploys. Set `ODDS_API_KEYS`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the Render dashboard. Run one worker only, because the scanner runs inside the web process.

The `Dockerfile` is for other hosts. Render uses its native Python runtime.

## Current status
- Only SportyBet is active (`SOFT_BOOKS=sportybet`). Its prematch and live feeds returned real data from Colab. The live path was checked once on a real game (NBA preseason). It has not yet been confirmed from Render's IP.
- SportyBet is fetched with urllib because it answers httpx requests with HTTP 202 and an empty body.
- Betano, BetKing, MSport and Nairabet have no default URL and stay idle until you set one. 1xBet and Paripesa are geo-blocked. Bet9ja needs `BET9JA_GROUPS` filled in.
- Only SportyBet has a live (in-play) fetcher. Other books get no live rows.
- Live alerts stay unproven until the model has been validated on logged games (see `data/live_log`).
- Team tiers are filled for nba, euroleague, acb, lnb and bbl. Every other league, and any team missing from the file, is treated as MID.
