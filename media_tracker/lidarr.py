"""Lidarr client for the dislike reaper.

Two jobs: find the track file backing a disliked song, and then remove it —
deleting the file through Lidarr (so its database stays consistent and the cron
host needs no access to the music share) and unmonitoring the album so Lidarr
does not simply fetch it again.

Note that Lidarr monitors at *album* granularity; there is no per-track
monitoring. Unmonitoring therefore stops the whole album from being re-fetched
or upgraded, which is the behaviour this project wants.
"""
import logging
import os
import unicodedata

import httpx

logger = logging.getLogger(__name__)

# A path match must line up on the filename, its album directory AND the artist
# directory above that. Two components is not enough: "Greatest Hits/01 Intro.flac"
# is a plausible path under any number of different artists, and matching on it
# would delete whichever one happened to be scanned first.
MIN_PATH_COMPONENTS = 3


class LidarrError(RuntimeError):
    pass


class Lidarr:
    def __init__(self, url: str, api_key: str, *, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"X-Api-Key": api_key},
        )
        self._artists = None          # cached artist list
        self._track_files = {}        # artist id -> track file list
        self._albums = {}             # artist id -> album list

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, method: str, path: str, **kwargs):
        response = self._client.request(method, f"{self.url}/api/v1/{path}", **kwargs)
        if response.status_code >= 400:
            raise LidarrError(
                f"{method} {path} -> {response.status_code}: {response.text[:200]}"
            )
        if not response.content:
            return None
        return response.json()

    def system_status(self) -> dict:
        """Raises if the URL or API key is wrong."""
        return self._request("GET", "system/status")

    def artists(self) -> list[dict]:
        if self._artists is None:
            self._artists = self._request("GET", "artist") or []
            logger.debug("loaded %d artists from lidarr", len(self._artists))
        return self._artists

    def track_files(self, artist_id: int) -> list[dict]:
        """Track files for one artist. Lidarr rejects an unscoped query, and
        per-artist keeps the response small enough to scan repeatedly."""
        if artist_id not in self._track_files:
            self._track_files[artist_id] = self._request(
                "GET", "trackfile", params={"artistId": artist_id}
            ) or []
        return self._track_files[artist_id]

    def find_track_file(self, *, path: str, artist: str | None,
                        album_artist: str | None = None) -> tuple[dict, dict] | None:
        """Locate the Lidarr track file for a path reported by Navidrome.

        Returns (track_file, lidarr_artist), or None if there is any doubt.

        Navidrome's path may be relative to its music folder while Lidarr's is
        absolute, and the two may be mounted differently, so the join is on
        trailing path components rather than on the string as a whole.

        Every ambiguity resolves to None. Failing to match is harmless — the song
        is reported and left alone — whereas a confident wrong match deletes
        somebody's file, so this refuses rather than guesses.
        """
        wanted = _components(path)
        # Require the artist/album/file triple, or the whole path if Navidrome
        # gave us a shorter one than that.
        required = min(len(wanted), MIN_PATH_COMPONENTS)
        if required < MIN_PATH_COMPONENTS:
            logger.warning("refusing to match on a %d-component path: %s",
                           len(wanted), path)
            return None

        candidates = self._candidate_artists(artist, album_artist, wanted)
        if not candidates:
            logger.warning("no lidarr artist matches %r; refusing to guess",
                           album_artist or artist)
            return None

        matches = []
        for candidate in candidates:
            for track_file in self.track_files(candidate["id"]):
                score = _suffix_score(wanted, _components(track_file.get("path", "")))
                if score >= required:
                    matches.append((score, candidate, track_file))
        if not matches:
            return None

        best = max(score for score, _, _ in matches)
        winners = [(a, tf) for score, a, tf in matches if score == best]

        # Distinct files tying at the top score means the path genuinely does not
        # identify one track. Never break the tie arbitrarily.
        unique = {tf["id"]: (a, tf) for a, tf in winners}
        if len(unique) > 1:
            logger.warning(
                "AMBIGUOUS: %s matches %d lidarr files equally well (%s); refusing",
                path, len(unique),
                ", ".join(tf.get("path", "?") for _, tf in unique.values()),
            )
            return None

        matched_artist, track_file = next(iter(unique.values()))

        # The path told us which file; confirm the artist independently, so a
        # coincidental path collision cannot carry us onto the wrong artist.
        expected = _artist_names(artist, album_artist)
        actual = _norm(matched_artist.get("artistName"))
        if expected and actual not in expected:
            logger.warning(
                "REFUSING %s: path matched lidarr artist %r but navidrome says %s",
                path, matched_artist.get("artistName"),
                " / ".join(sorted(n for n in (artist, album_artist) if n)),
            )
            return None

        logger.debug("matched %s -> %s (%d components, artist %s)",
                     path, track_file.get("path"), best,
                     matched_artist.get("artistName"))
        return track_file, matched_artist

    def _candidate_artists(self, artist, album_artist, wanted_components):
        """Narrow to the artists this file could plausibly belong to.

        A name match on the track or album artist first, then artists whose
        library folder appears in the path. If neither identifies anyone we
        return nothing: scanning the whole library would widen the search
        precisely when we are least sure who the artist is, which is exactly
        when a coincidental path collision would do damage.
        """
        names = _artist_names(artist, album_artist)
        by_name = [a for a in self.artists() if _norm(a.get("artistName")) in names]
        if by_name:
            return by_name

        folders = {_norm(component) for component in wanted_components[:-1]}
        by_folder = [
            a for a in self.artists()
            if _norm(os.path.basename((a.get("path") or "").rstrip("/"))) in folders
        ]
        if by_folder:
            logger.debug("narrowed by library folder rather than name: %s",
                         [a.get("artistName") for a in by_folder])
        return by_folder

    def albums(self, artist_id: int) -> list[dict]:
        """Albums Lidarr holds for one artist. Used by --diagnose to tell
        "Lidarr has never heard of this album" apart from "it has the album but
        not this file", which need different fixes."""
        if artist_id not in self._albums:
            self._albums[artist_id] = self._request(
                "GET", "album", params={"artistId": artist_id}
            ) or []
        return self._albums[artist_id]

    def delete_track_file(self, track_file_id: int) -> None:
        """Delete the file from disk. Honours Lidarr's recycle bin if configured."""
        self._request("DELETE", f"trackfile/{track_file_id}")

    def album(self, album_id: int) -> dict:
        return self._request("GET", f"album/{album_id}")

    def set_album_monitored(self, album_id: int, monitored: bool) -> None:
        """Flip an album's monitored flag via the bulk endpoint, which is the one
        Lidarr's own UI uses and does not require round-tripping the full album."""
        self._request(
            "PUT", "album/monitor",
            json={"albumIds": [album_id], "monitored": monitored},
        )


def _components(path: str) -> list[str]:
    return [part for part in path.replace("\\", "/").split("/") if part]


# Navidrome joins multiple credited artists into one field. Splitting on these
# lets "Foxes;Jonny Harris" match the lidarr artist "Foxes". Deliberately not
# splitting on "&" or ",", which appear inside real artist names.
_ARTIST_SEPARATORS = (";", "\u2022", "/")


def _artist_names(*values) -> set[str]:
    """Every individual artist name mentioned, normalised.

    Includes the unsplit original, so an artist whose real name contains a
    separator still matches.
    """
    names = set()
    for value in values:
        if not value:
            continue
        names.add(_norm(value))
        parts = [value]
        for separator in _ARTIST_SEPARATORS:
            parts = [bit for part in parts for bit in part.split(separator)]
        names.update(_norm(part) for part in parts if part.strip())
    return names


def _norm(value: str | None) -> str:
    """Case- and unicode-fold for comparison.

    NFC matters because a library written on macOS stores accented filenames
    decomposed; without it "Bj\u00f6rk" and "Bjo\u0308rk" would not compare equal.
    Normalising can only make two spellings of the same name match, never two
    different names, so it cannot manufacture a false positive.
    """
    return unicodedata.normalize("NFC", (value or "").strip()).casefold()


def _suffix_score(a: list[str], b: list[str]) -> int:
    """Number of trailing path components two paths share, compared case-insensitively."""
    score = 0
    for left, right in zip(reversed(a), reversed(b)):
        if _norm(left) != _norm(right):
            break
        score += 1
    return score
