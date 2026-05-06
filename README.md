# MLB Pitcher Bot

Posts the final pitching line for every MLB starting pitcher to Bluesky, as soon as their line is official.

**Example post:**
```
Jacob deGrom (CLE) @NYY: 6.1 IP 7 H 6 ER 1 BB 7 K 94 pitches. Left in bottom of 7th with 2 runners on (2 scored).
```

## How It Works

The bot polls the MLB Stats API every 90 seconds. For each active game, it:
1. Identifies both starting pitchers (first pitcher listed in boxscore)
2. Tracks which runners on base when the SP exits are their "statistical responsibility"
3. Waits until the SP's line is truly final (see logic below)
4. Posts once to Bluesky, then records the game+pitcher combo so it never posts twice

### "Final Line" Logic
A SP's line is final when **all** of:
- The SP is no longer the current pitcher, **and**
- One of: no inherited runners remain on base / all responsible runners scored or were retired / the inning the SP was removed in has ended
- Special case: complete games become final when the game ends

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env`:
```
BLUESKY_HANDLE=yourbothandle.bsky.social
BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
```

Get an app password from **Bluesky → Settings → App Passwords** (do not use your main password).

### 3. Run locally

**Continuous mode** (polls every 90 seconds):
```bash
python bot.py
```

**Single pass** (useful for cron or testing):
```bash
python bot.py --once
```

**Dry run** (no posts sent, just logs):
```bash
DRY_RUN=true python bot.py --once
```

**Test the API layer against a specific date:**
```bash
python mlb_api.py 2025-09-15
```

### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `BLUESKY_HANDLE` | Yes | — | Your bot's Bluesky handle |
| `BLUESKY_APP_PASSWORD` | Yes | — | App password from Bluesky settings |
| `POLL_INTERVAL` | No | `90` | Seconds between polls (continuous mode) |
| `DRY_RUN` | No | `false` | Set to `true` to skip posting |
| `STATE_FILE` | No | `posted_starters.json` | Path to state persistence file |

## Deploying to GitHub Actions

The workflow in `.github/workflows/bot.yml` runs every 5 minutes during MLB game hours (12pm–1am ET).

### Setup

1. Fork or push this repo to GitHub.

2. Add secrets under **Settings → Secrets and variables → Actions**:
   - `BLUESKY_HANDLE`
   - `BLUESKY_APP_PASSWORD`

3. Enable Actions if needed (**Actions** tab → **I understand my workflows, go ahead and enable them**).

4. The workflow uses GitHub Actions cache to persist `posted_starters.json` between runs, so the bot won't re-post after restarts.

### Manual trigger

Go to **Actions → Pitcher Bot → Run workflow** to trigger a single pass immediately.

## File Overview

| File | Purpose |
|---|---|
| `bot.py` | Main polling loop and game orchestration |
| `mlb_api.py` | MLB Stats API client, pitcher state parsing, post formatting |
| `bluesky_client.py` | Bluesky auth and posting |
| `state.py` | JSON state file — tracks which SP lines have been posted |
| `posted_starters.json` | Auto-created on first run |
| `.github/workflows/bot.yml` | GitHub Actions deployment |

## Notes

- Only Regular Season (`R`) and Playoff (`P`, `F`, `D`, `L`, `W`) games are processed. Spring Training and exhibitions are skipped.
- Errors fetching a single game are caught and logged; other games continue processing.
- State entries older than 2 days are pruned automatically.
- The bot uses the MLB Stats API (statsapi.mlb.com) — no API key required.
