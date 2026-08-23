"""Navidrome like/dislike prompts.

The Navidrome side of media_tracker. A sandboxed WebAssembly plugin running
inside Navidrome POSTs a completed play to /scrobble; we DM a Like/Dislike
prompt and store the answer when the button comes back through /slack.

The plugin cannot receive a Slack click or wait for a reply, which is why the
interactive half lives here alongside the Jellyfin flow rather than in the
plugin itself.
"""
import hmac
import logging
import uuid
from pathlib import Path

import db
from helpers import load_config, slack_dm

logger = logging.getLogger(__name__)

SOURCE = "navidrome"
RATING_LIKE = "like"
RATING_DISLIKE = "dislike"

_database = None


def _config():
    return load_config()


def database() -> db.Database:
    """Open the ratings DB on first use and reuse it thereafter."""
    global _database
    if _database is None:
        config_obj = _config()
        path = config_obj.get("navidrome", "db_path", fallback="ratings.db")
        # Relative paths resolve against the repo root, next to config.ini.
        if not Path(path).is_absolute():
            path = str(Path(__file__).resolve().parent.parent / path)
        _database = db.Database(path)
        logger.info("navidrome ratings db: %s", path)
    return _database


def secret_is_valid(provided: str) -> bool:
    """Constant-time check of the shared secret the plugin sends."""
    expected = _config().get("navidrome", "plugin_shared_secret", fallback="")
    if not expected:
        logger.error("navidrome.plugin_shared_secret is not configured; rejecting")
        return False
    return hmac.compare_digest(provided or "", expected)


def _track_line(title, artist):
    title = title or "Unknown title"
    if artist:
        return f"*{title}*\nby {artist}"
    return f"*{title}*"


def _prompt_blocks(event_id, title, artist, album, playlist_context):
    context_bits = []
    if album:
        context_bits.append(f"Album: {album}")
    if playlist_context:
        context_bits.append(f"From: {playlist_context}")

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"You just finished listening to:\n{_track_line(title, artist)}\n"
                        "Did you like it?",
            },
        }
    ]
    if context_bits:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "  ·  ".join(context_bits)}],
        })
    blocks.append({
        "type": "actions",
        "block_id": "navidrome_rating",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "👍 Like", "emoji": True},
                "style": "primary",
                "value": f"{SOURCE}.{RATING_LIKE}.{event_id}",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "👎 Dislike", "emoji": True},
                "style": "danger",
                "value": f"{SOURCE}.{RATING_DISLIKE}.{event_id}",
            },
        ],
    })
    return blocks


def _answered_blocks(title, artist, rating):
    verb = "liked 👍" if rating == RATING_LIKE else "disliked 👎"
    return [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"You {verb}:\n{_track_line(title, artist)}"},
    }]


def handle_scrobble(payload):
    """Handle a completed play forwarded by the plugin.

    Returns a short dict describing what happened, for the HTTP response.
    """
    navidrome_user = payload.get("user")
    if not navidrome_user:
        return {"status": "error", "reason": "missing user"}, 400

    config_obj = _config()
    slack_user = config_obj.get("media_tracker", "slack_user_id")
    title = payload.get("title")
    artist = payload.get("artist")
    album = payload.get("album")
    track_id = payload.get("track_id")

    store = database()

    # Don't pester about a song this user has already rated.
    if store.has_answer(navidrome_user, track_id=track_id, title=title, artist=artist):
        logger.info("already answered for %r; skipping", track_id or title)
        return {"status": "skipped", "reason": "already answered"}, 202

    event_id = str(uuid.uuid4())
    store.insert_pending(
        event_id=event_id,
        navidrome_user=navidrome_user,
        slack_user=slack_user,
        track_id=track_id,
        title=title,
        artist=artist,
        album=album,
        played_at=payload.get("timestamp"),
        playlist_context=payload.get("playlist_context"),
    )

    try:
        client, dm_channel = slack_dm()
        response = client.chat_postMessage(
            channel=dm_channel,
            text="Did you like the last song?",  # notification fallback
            blocks=_prompt_blocks(event_id, title, artist, album,
                                  payload.get("playlist_context")),
        )
        store.set_slack_message(event_id, dm_channel, response["ts"])
    except Exception:
        # The play is already recorded; never fail the plugin's request over Slack.
        logger.exception("failed to post Slack prompt for event %s", event_id)
        return {"status": "stored", "slack": "failed", "event_id": event_id}, 502

    return {"status": "ok", "event_id": event_id}, 200


def handle_rating(rating, event_id):
    """Handle a Like/Dislike click. Edits the original message in place so the
    buttons disappear; a second click on a stale message is a no-op."""
    if rating not in (RATING_LIKE, RATING_DISLIKE):
        logger.warning("unknown navidrome rating %r", rating)
        return

    store = database()
    if not store.set_rating(event_id, rating):
        logger.info("ignoring duplicate/late click for event %s", event_id)
        return

    row = store.get(event_id)
    if row is None or not row["slack_ts"]:
        return
    try:
        client, _ = slack_dm()
        client.chat_update(
            channel=row["slack_channel"],
            ts=row["slack_ts"],
            text="Thanks for the feedback!",
            blocks=_answered_blocks(row["title"], row["artist"], rating),
        )
    except Exception:
        # Updating the message is cosmetic; the rating is already stored.
        logger.exception("failed to update Slack message for event %s", event_id)
