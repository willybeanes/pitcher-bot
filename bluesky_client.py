"""
Bluesky AT Protocol client — authentication and posting.
"""

import logging
import os

from atproto import Client

log = logging.getLogger(__name__)

_client: Client | None = None


def get_client() -> Client:
    """Return an authenticated atproto Client, creating one if needed."""
    global _client
    if _client is not None:
        return _client

    handle = os.environ.get("BLUESKY_HANDLE", "").strip()
    password = os.environ.get("BLUESKY_APP_PASSWORD", "").strip()

    if not handle or not password:
        raise RuntimeError(
            "BLUESKY_HANDLE and BLUESKY_APP_PASSWORD environment variables must be set."
        )

    log.info("Logging in as %r (password length: %d)", handle, len(password))
    client = Client()
    client.login(handle, password)
    log.info("Logged in to Bluesky as %s", handle)
    _client = client
    return _client


def post_text(text: str, dry_run: bool = False) -> bool:
    """
    Post text to Bluesky. Returns True on success.
    If dry_run is True, just logs the post without sending.
    """
    if dry_run:
        log.info("[DRY RUN] Would post: %s", text)
        return True

    try:
        client = get_client()
        client.send_post(text=text)
        log.info("Posted: %s", text)
        return True
    except Exception as exc:
        log.error("Failed to post to Bluesky: %s", exc)
        return False
