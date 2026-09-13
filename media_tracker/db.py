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
    responded_at     TEXT,
    reaped_at        TEXT,                             -- when the reaper acted
    reap_status      TEXT,                             -- deleted | missing | unmatched | failed
    reaped_path      TEXT,                             -- the file we actually removed
    reap_attempts    INTEGER NOT NULL DEFAULT 0        -- bounds retries of transient errors
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
            self._migrate(conn)

    @staticmethod
    def _migrate(conn) -> None:
        """Add columns introduced after the first databases were created.

        SQLite has no ADD COLUMN IF NOT EXISTS, so check what is already there.
        """
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(ratings)")}
        for column in ("reaped_at", "reap_status", "reaped_path"):
            if column not in existing:
                conn.execute(f"ALTER TABLE ratings ADD COLUMN {column} TEXT")
        if "reap_attempts" not in existing:
            conn.execute(
                "ALTER TABLE ratings ADD COLUMN reap_attempts INTEGER NOT NULL DEFAULT 0"
            )

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

    def unreaped_dislikes(self, max_attempts: int) -> list[sqlite3.Row]:
        """The reaper's work queue: disliked songs not yet dealt with, oldest first.

        Two things keep this queue from growing without bound. A row that reaches
        a terminal outcome gets `reaped_at` set and never returns. A row that
        keeps hitting transient errors is retried only `max_attempts` times, after
        which it is excluded here and marked `failed` by the caller — otherwise a
        song that fails for a durable reason that merely looks transient would be
        retried on every run forever.
        """
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM ratings
                WHERE rating = 'dislike' AND reaped_at IS NULL AND reap_attempts < ?
                ORDER BY responded_at
                """,
                (max_attempts,),
            ).fetchall()

    def mark_reaped(self, event_id: str, status: str, path: str | None = None) -> None:
        """Record a terminal outcome. `path` is the file that was actually removed,
        kept as an audit trail of what the job has deleted."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE ratings
                SET reaped_at = ?, reap_status = ?, reaped_path = COALESCE(?, reaped_path)
                WHERE event_id = ?
                """,
                (_now(), status, path, event_id),
            )

    def record_attempt(self, event_id: str) -> int:
        """Count a failed attempt and return the new total, so the caller can tell
        when a row has exhausted its retries."""
        with _lock, self._connect() as conn:
            conn.execute(
                "UPDATE ratings SET reap_attempts = reap_attempts + 1 WHERE event_id = ?",
                (event_id,),
            )
            row = conn.execute(
                "SELECT reap_attempts FROM ratings WHERE event_id = ?", (event_id,)
            ).fetchone()
            return row["reap_attempts"] if row else 0

    def reaped_with_status(self, status: str) -> list[sqlite3.Row]:
        """Rows already recorded with a given reap_status, for --retry.

        Re-processing overwrites the old mark, so a song that has since been
        imported into Lidarr gets a second chance without being retried on every
        single run in the meantime.
        """
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM ratings WHERE rating = 'dislike' AND reap_status = ?"
                " ORDER BY responded_at",
                (status,),
            ).fetchall()

    def clear_reap_state(self, event_ids: list[str]) -> None:
        """Put rows back in the queue with a fresh retry budget, for --retry."""
        if not event_ids:
            return
        placeholders = ",".join("?" * len(event_ids))
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE ratings
                SET reaped_at = NULL, reap_status = NULL, reap_attempts = 0
                WHERE event_id IN ({placeholders})
                """,
                event_ids,
            )

    def reap_summary(self) -> list[sqlite3.Row]:
        """Counts per reap_status, for --status."""
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT COALESCE(reap_status, 'queued') AS status, COUNT(*) AS n
                FROM ratings WHERE rating = 'dislike'
                GROUP BY 1 ORDER BY 1
                """
            ).fetchall()
