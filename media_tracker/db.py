"""SQLite persistence for Navidrome song ratings.

One table, `ratings`, holds one row per completed play. A row starts as
`pending` when we send the Slack prompt and moves to `answered` once the button
is clicked. Uses the stdlib sqlite3 module with a short-lived connection per
operation, so it is safe across Flask's threaded request handling.
"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ratings (
    event_id         TEXT PRIMARY KEY,
    navidrome_user   TEXT NOT NULL,
    slack_user       TEXT NOT NULL,
    track_id         TEXT,
    title            TEXT,
    artist           TEXT,
    album            TEXT,
    played_at        TEXT,
    playlist_context TEXT,
    rating           TEXT,                             -- 'like' | 'dislike' | NULL
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending | answered
    slack_channel    TEXT,
    slack_ts         TEXT,
    created_at       TEXT NOT NULL,
    responded_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ratings_track
    ON ratings (navidrome_user, track_id);
"""

# Serialise the read-modify-write in set_rating across threads.
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def insert_pending(
        self,
        *,
        event_id: str,
        navidrome_user: str,
        slack_user: str,
        track_id: str | None,
        title: str | None,
        artist: str | None,
        album: str | None,
        played_at: str | None,
        playlist_context: str | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ratings (
                    event_id, navidrome_user, slack_user, track_id, title, artist,
                    album, played_at, playlist_context, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    event_id, navidrome_user, slack_user, track_id, title, artist,
                    album, played_at, playlist_context, _now(),
                ),
            )

    def set_slack_message(self, event_id: str, channel: str, ts: str) -> None:
        """Remember where the prompt landed so we can edit it after the answer."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE ratings SET slack_channel = ?, slack_ts = ? WHERE event_id = ?",
                (channel, ts, event_id),
            )

    def set_rating(self, event_id: str, rating: str) -> bool:
        """Record a rating. Returns True if this was the first answer, False if the
        row was missing or already answered (so a second click is a no-op)."""
        with _lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM ratings WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None or row["status"] == "answered":
                return False
            conn.execute(
                """
                UPDATE ratings
                SET rating = ?, status = 'answered', responded_at = ?
                WHERE event_id = ?
                """,
                (rating, _now(), event_id),
            )
            return True

    def get(self, event_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM ratings WHERE event_id = ?", (event_id,)
            ).fetchone()

    def has_answer(
        self,
        navidrome_user: str,
        *,
        track_id: str | None,
        title: str | None,
        artist: str | None,
    ) -> bool:
        """True if this user already answered for this song, so we skip re-prompting.
        Matches on track_id when available (the reliable key), else title+artist."""
        with self._connect() as conn:
            if track_id:
                row = conn.execute(
                    """
                    SELECT 1 FROM ratings
                    WHERE navidrome_user = ? AND track_id = ? AND rating IS NOT NULL
                    LIMIT 1
                    """,
                    (navidrome_user, track_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT 1 FROM ratings
                    WHERE navidrome_user = ? AND title IS ? AND artist IS ?
                      AND rating IS NOT NULL
                    LIMIT 1
                    """,
                    (navidrome_user, title, artist),
                ).fetchone()
            return row is not None
