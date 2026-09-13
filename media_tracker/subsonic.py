"""Minimal Subsonic client, used to resolve a Navidrome track_id to a file path.

The reaper needs the on-disk path of a disliked song, and `track_id` in
`ratings.db` is Navidrome's own media-file ID — meaningless to Lidarr. Subsonic's
`getSong` is the one place that maps the two, so this is deliberately a
three-endpoint client rather than a general-purpose library.

Auth is the standard salted-token scheme (`t = md5(password + salt)`), so the
password never crosses the wire.
"""
import hashlib
import logging
import secrets
import unicodedata

import httpx

logger = logging.getLogger(__name__)

# The API version Navidrome advertises. Bumping it gains us nothing here.
API_VERSION = "1.16.1"
CLIENT_NAME = "media-tracker-reaper"


class SubsonicError(RuntimeError):
    """The server answered, but with status=failed."""


class NotFound(SubsonicError):
    """Error code 70 — the requested id is gone from the library."""


class Subsonic:
    def __init__(self, url: str, username: str, password: str, *, timeout: float = 20.0):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _auth_params(self) -> dict:
        salt = secrets.token_hex(8)
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": API_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }

    def _get(self, endpoint: str, **params) -> dict:
        response = self._client.get(
            f"{self.url}/rest/{endpoint}",
            params={**self._auth_params(), **params},
        )
        response.raise_for_status()
        body = response.json()["subsonic-response"]
        if body.get("status") == "failed":
            error = body.get("error", {})
            message = f"{endpoint}: {error.get('message', 'unknown error')}"
            if error.get("code") == 70:
                raise NotFound(message)
            raise SubsonicError(message)
        return body

    def ping(self) -> None:
        """Raises if the server is unreachable or the credentials are wrong."""
        self._get("ping")

    def get_song(self, track_id: str) -> dict:
        """Return the song record for a Navidrome media-file ID.

        Raises NotFound if the track is no longer in the library — which, for the
        reaper, means the file is already gone.
        """
        return self._get("getSong", id=track_id)["song"]

    def search_song(self, *, title: str | None, artist: str | None,
                    album: str | None) -> dict | None:
        """Lookup for rows whose track_id is missing or stale.

        Requires title, artist AND album to match, and requires the match to be
        unique. Title+artist alone is not enough to identify a file: the same
        song routinely appears on a studio album, a compilation and a live
        record, and picking one of those arbitrarily would delete a recording
        that was never rated.

        Returns None unless exactly one song matches.
        """
        if not (title and artist and album):
            # Without all three we cannot tell recordings apart, so don't try.
            return None
        result = self._get("search3", query=f"{artist} {title}", songCount=50,
                           artistCount=0, albumCount=0)
        hits = [
            song for song in result.get("searchResult3", {}).get("song", [])
            if _norm(song.get("title")) == _norm(title)
            and _norm(song.get("artist")) == _norm(artist)
            and _norm(song.get("album")) == _norm(album)
        ]
        if len(hits) != 1:
            if hits:
                logger.warning("%r by %r on %r matches %d navidrome songs; refusing",
                               title, artist, album, len(hits))
            return None
        return hits[0]


def _norm(value: str | None) -> str:
    return unicodedata.normalize("NFC", (value or "").strip()).casefold()
