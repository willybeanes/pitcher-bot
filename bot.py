"""
Main polling loop — monitors live MLB games and posts final SP lines to Bluesky.
"""

import logging
import os
import sys
import time
from datetime import date, timezone, datetime

from dotenv import load_dotenv

import mlb_api
import bluesky_client
import state as state_store

load_dotenv()

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "90"))  # seconds
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# In-memory game state: gamePk -> GamePitcherState
_game_states: dict[int, mlb_api.GamePitcherState] = {}


def _today() -> str:
    if override := os.environ.get("MLB_DATE", "").strip():
        return override
    utc_now = datetime.now(timezone.utc)
    et_hour = (utc_now.hour - 4) % 24
    if et_hour < 6:
        from datetime import timedelta
        return (date.today() - timedelta(days=1)).isoformat()
    return date.today().isoformat()


def process_game(game_pk: int, game_type: str) -> None:
    """Fetch live data for one game and check both SPs for a final line."""
    live_data = mlb_api.fetch_live_feed(game_pk)
    if not live_data:
        return

    game_data = live_data.get("gameData", {})
    status = game_data.get("status", {}).get("detailedState", "")

    # Only process in-progress or finished games
    if status not in (
        "In Progress", "Final", "Game Over", "Completed Early",
        "Manager challenge", "Delay", "Delay: Rain", "Delay: Other",
    ):
        log.debug("gamePk=%s skipped (status=%s)", game_pk, status)
        return

    prev_state = _game_states.get(game_pk)
    new_state = mlb_api.parse_game_state(game_pk, live_data, prev_state)
    _game_states[game_pk] = new_state

    for side in ("home", "away"):
        sp_info = new_state.starting_pitcher.get(side)
        if not sp_info:
            continue

        sp_id = sp_info["id"]

        if state_store.already_posted(game_pk, sp_id):
            continue

        if mlb_api._is_sp_line_final(side, new_state):
            line = mlb_api.build_pitcher_line(side, new_state, live_data)
            if line is None:
                log.warning("Could not build line for gamePk=%s side=%s", game_pk, side)
                continue

            post_text = mlb_api.format_post(line)
            log.info("FINAL LINE — %s", post_text)

            success = bluesky_client.post_text(post_text, dry_run=DRY_RUN)
            if success:
                state_store.mark_posted(game_pk, sp_id)


def run_once() -> None:
    """Single pass: fetch schedule, process all active games."""
    today = _today()
    log.info("Polling schedule for %s", today)

    games = mlb_api.fetch_schedule(today)
    valid_games = [
        g for g in games
        if g.get("gameType") in mlb_api.VALID_GAME_TYPES
    ]
    log.info("Found %d valid games (of %d total)", len(valid_games), len(games))

    for g in valid_games:
        game_pk = g.get("gamePk")
        game_type = g.get("gameType", "R")
        status = g.get("status", {}).get("detailedState", "")

        # Skip games that haven't started or were postponed
        if status in ("Scheduled", "Pre-Game", "Warmup", "Postponed", "Cancelled", "Suspended"):
            continue

        try:
            process_game(game_pk, game_type)
        except Exception as exc:
            log.error("Error processing gamePk=%s: %s", game_pk, exc, exc_info=True)


def run_loop() -> None:
    """Continuous polling loop."""
    if DRY_RUN:
        log.info("=== DRY RUN MODE — no posts will be sent ===")

    state_store.prune_old_entries()

    while True:
        try:
            run_once()
        except Exception as exc:
            log.error("Unexpected error in run_once: %s", exc, exc_info=True)

        log.info("Sleeping %ds until next poll...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    # Allow `python bot.py --once` for cron/GHA single-shot runs
    if "--once" in sys.argv:
        state_store.prune_old_entries()
        run_once()
    else:
        run_loop()
