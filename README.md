# Tennis Alpha Live Odds Scraper

A cloud-based, fully automated tennis odds scraper, results backfiller, and database archiver.

## Tech Stack
* **Automation**: GitHub Actions (runs every 15 minutes, 2 hours, and daily)
* **Storage**: Supabase PostgreSQL database
* **Source**: The-Odds-API (v4)

## Architecture
1. **Live Poller**: Rotates 21 free API keys, tracks schedule, and polls odds closer to match start to secure the Closing Line.
2. **Results Backfiller**: Sweeps global results to update winners and scores.
3. **Database Archiver**: Sends snapshots above 400MB to Telegram in CSV format and cleans the database to fit Supabase's free tier.
