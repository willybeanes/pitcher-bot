"""
Redis-based persistence for tracking which SP lines have already been posted.
Uses Upstash Redis REST API — no extra dependencies beyond `requests`.
Falls back to in-memory dict if Redis env vars are not set (useful for local testing).
"""

import logging
import os
from datetime import date, timedelta

import requests

log = logging.getLogger(__name__)

REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
EXPIRY_DAYS = 2
EXPIRY_SECONDS = EXPIRY_DAYS * 24 * 60 * 60

# Fallback in-memory store when Redis is not configured
_memory: dict[str, str] = {}


def _redis_available() -> bool:
    return bool(REDIS_URL and REDIS_TOKEN)


def _headers() -> dict:
    return {"Authorization": f"Bearer {REDIS_TOKEN}"}


def _redis_get(key: str) -> str | None:
    try:
        r = requests.get(f"{REDIS_URL}/get/{key}", headers=_headers(), timeout=5)
        data = r.json()
        return data.get("result")  # None if key doesn't exist
    except Exception as exc:
        log.warning("Redis GET failed for %s: %s", key, exc)
        return None


def _redis_set(key: str, value: str, ex: int) -> None:
    try:
        requests.get(f"{REDIS_URL}/set/{key}/{value}/ex/{ex}", headers=_headers(), timeout=5)
    except Exception as exc:
        log.warning("Redis SET failed for %s: %s", key, exc)


def _entry_key(game_pk: int, pitcher_id: int) -> str:
    return f"posted:{game_pk}:{pitcher_id}"


def already_posted(game_pk: int, pitcher_id: int) -> bool:
    key = _entry_key(game_pk, pitcher_id)
    if _redis_available():
        return _redis_get(key) is not None
    return key in _memory


def mark_posted(game_pk: int, pitcher_id: int) -> None:
    key = _entry_key(game_pk, pitcher_id)
    value = date.today().isoformat()
    if _redis_available():
        _redis_set(key, value, ex=EXPIRY_SECONDS)
        log.info("Redis: marked %s as posted", key)
    else:
        _memory[key] = value
        log.info("Memory: marked %s as posted", key)


def prune_old_entries() -> None:
    """No-op for Redis (TTL handles expiry automatically). Cleans memory fallback."""
    if not _redis_available():
        cutoff = date.today() - timedelta(days=EXPIRY_DAYS)
        before = len(_memory)
        for k in list(_memory):
            try:
                if date.fromisoformat(_memory[k]) < cutoff:
                    del _memory[k]
            except Exception:
                del _memory[k]
        pruned = before - len(_memory)
        if pruned:
            log.info("Pruned %d old in-memory state entries", pruned)
