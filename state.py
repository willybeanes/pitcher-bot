"""
JSON-based persistence for tracking which SP lines have already been posted.
"""

import json
import logging
import os
from datetime import date, timedelta

log = logging.getLogger(__name__)

STATE_FILE = os.environ.get("STATE_FILE", "posted_starters.json")
EXPIRY_DAYS = 2


def _load() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Could not read state file: %s", exc)
        return {}


def _save(data: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.error("Could not write state file: %s", exc)


def _key(game_pk: int, pitcher_id: int) -> str:
    return f"{game_pk}:{pitcher_id}"


def already_posted(game_pk: int, pitcher_id: int) -> bool:
    data = _load()
    return _key(game_pk, pitcher_id) in data


def mark_posted(game_pk: int, pitcher_id: int) -> None:
    data = _load()
    data[_key(game_pk, pitcher_id)] = date.today().isoformat()
    _save(data)


def prune_old_entries() -> None:
    """Remove entries older than EXPIRY_DAYS days."""
    data = _load()
    cutoff = date.today() - timedelta(days=EXPIRY_DAYS)
    pruned = {k: v for k, v in data.items() if _parse_date(v) >= cutoff}
    if len(pruned) < len(data):
        log.info("Pruned %d old state entries", len(data) - len(pruned))
    _save(pruned)


def _parse_date(d: str) -> date:
    try:
        return date.fromisoformat(d)
    except Exception:
        return date.min
