# Allocate Shift Scraper

A Playwright-powered scraper that logs into Allocate/Loop, collects all available bank duties across every period, filters them using configurable rules, and emails when new matching shifts appear. Designed to run on GitHub Actions every 15 minutes with random jitter to avoid fixed timing.

## Features

- Logs into Allocate/Loop using Playwright and persists the authenticated storage state for reuse.
- Iterates over all periods and paginated results to capture every available duty.
- Deduplicates by request ID and remembers previously seen shifts between runs.
- Applies YAML-defined rules to categorise shifts (priority, late/night, ignore).
- Sends email notifications only when new priority or late/night shifts are discovered.
- GitHub Actions workflow schedules the scraper every 15 minutes (with jitter) and runs with credentials supplied via repository secrets.

## Configuration

1. Copy `.env.example` to `.env` (optional for local runs) and populate the Allocate and SMTP credentials.
2. Update `rules.yaml` to tailor which shifts should trigger notifications.
3. Set the following GitHub Actions secrets:
   - `ALLOCATE_USER`, `ALLOCATE_PASS`
   - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`
   - `SMTP_FROM`, `SMTP_TO`

## Running Locally

```bash
pip install -r requirements.txt
python -m playwright install chromium
export $(cat .env | xargs)  # or use python-dotenv
python scraper.py
```

## GitHub Actions

The workflow in `.github/workflows/scraper.yml` installs dependencies, runs the scraper with secrets, and commits any updated state files (`seen_ids.json`, `storage_state.json`) back to the repository.
