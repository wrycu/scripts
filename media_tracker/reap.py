#!/usr/bin/env python3
"""Clear judged songs out of the library, so Navidrome only plays unjudged ones.

Run on a schedule. For every rated song in ratings.db that has not been dealt
with yet:

  1. resolve the Navidrome track_id to a file path (Subsonic `getSong`),
  2. find the matching Lidarr track file by path,
  3. dislike: delete it through Lidarr;
     like:    copy it into `liked_root`, then delete the original through Lidarr,
              recording the MusicBrainz ids needed to re-add it later,
  4. unmonitor the album so neither is re-grabbed.

Deleting through Lidarr rather than with os.remove keeps Lidarr's database
honest. Disliked songs need no access to the music share; liked songs do, since
the file has to be copied somewhere.

Lidarr has no per-track monitoring, so step 4 unmonitors the *whole album* —
nothing else on that album will be fetched or upgraded afterwards.

Pick what to act on with --disliked and/or --liked; songs with the other
rating are left queued, untouched. Dry run by default; pass --apply to actually
delete anything.

    python reap.py --disliked --liked                  # show what would happen
    python reap.py --disliked --apply                  # delete disliked songs only
    python reap.py --liked --apply --limit 5           # move liked songs, cautiously
"""
import argparse
import logging
import os
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import NamedTuple

import lidarr as lidarr_api
import navidrome
import subsonic as subsonic_api
from helpers import load_config

logger = logging.getLogger("reap")

# reap_status values. Only TRANSIENT failures are left unmarked, so they are
# retried on the next run; everything else is terminal and recorded.
STATUS_DELETED = "deleted"      # removed via lidarr, album unmonitored
STATUS_DELETED_DISK = "deleted-on-disk"  # removed directly; lidarr does not track it
STATUS_KEPT = "kept"            # liked: moved to liked_root via lidarr, album unmonitored
STATUS_KEPT_DISK = "kept-on-disk"  # liked: moved directly; lidarr does not track it
STATUS_MISSING = "missing"      # already gone from Navidrome; nothing to delete
STATUS_UNMATCHED = "unmatched"  # Navidrome knows it, Lidarr does not
STATUS_FAILED = "failed"        # kept erroring; gave up after MAX_ATTEMPTS
STATUS_OUT_OF_SCOPE = "out-of-scope"  # a navidrome library this job does not manage

# Not a reap_status: a sentinel meaning "the composed path is not on disk".
# Deliberately never recorded on its own. An absent file is only proof of an
# earlier deletion once we have seen the path scheme work at least once this
# run; until then it is just as likely that music_root is wrong, the library is
# not mounted, or navidrome is reporting synthesized paths rather than real ones.
SUSPECT_MISSING = object()

# Navidrome is reporting tag-derived filenames. A configuration problem with an
# exact fix, so it aborts immediately rather than after a run of failures.
SYNTHESIZED_PATH = object()

# Dry-run sentinel: this song WOULD have been acted on. Lets --limit ration
# actions identically whether or not --apply is set.
WOULD_ACT = object()

# Abort after this many consecutive absences with nothing yet deleted from disk.
SUSPECT_LIMIT = 10

RETRYABLE = (STATUS_UNMATCHED, STATUS_FAILED)

# Outcomes that changed the library, and so count towards --limit.
ACTED = (STATUS_DELETED, STATUS_DELETED_DISK, STATUS_KEPT, STATUS_KEPT_DISK)

# How many times a song may fail transiently before we stop queueing it. Without
# a cap, a song that fails for a durable reason that merely looks transient is
# retried on every run for ever, and those pile up.
MAX_ATTEMPTS = 5


def _norm(value):
    return unicodedata.normalize("NFC", (value or "").strip()).casefold()


def _loose(value):
    """Fold away punctuation for comparing titles between two catalogues.

    Lidarr takes titles from MusicBrainz while Navidrome takes them from tags,
    so the same album is routinely "Greatest Hits I, II & III: The Platinum
    Collection" in one and "Greatest Hits I II & III-The Platinum Collection" in
    the other. Punctuation is replaced with spaces rather than removed, so
    "III-The" does not become "iiithe".
    """
    folded = "".join(c if c.isalnum() else " " for c in _norm(value))
    return " ".join(folded.split())


def song_matches_row(song, row) -> bool:
    """Check that Navidrome handed us back the song that was actually rated.

    A track_id is only a pointer, and a rescanned or restored library can reissue
    one to a different file. Comparing what came back against what we stored
    turns that from a silent wrong deletion into a logged refusal.
    """
    for field in ("title", "artist", "album"):
        stored, returned = row[field], song.get(field)
        # Only judge fields we actually recorded at the time.
        if stored and returned and _norm(stored) != _norm(returned):
            logger.warning("track_id %s now resolves to a different song "
                           "(%s %r != stored %r); refusing",
                           row["track_id"], field, returned, stored)
            return False
    return True


def resolve_song(subsonic, row):
    """Find the Navidrome song for a rating row.

    Returns (song, None) on success, or (None, status) explaining the refusal.
    "Gone from the library" and "we declined to identify it" are deliberately
    different outcomes: the first means there is nothing left to delete, the
    second means there might be and we would not risk guessing.
    """
    track_id = row["track_id"]
    if track_id:
        try:
            song = subsonic.get_song(track_id)
        except subsonic_api.NotFound:
            logger.info("track_id %s is gone from navidrome; trying title/artist",
                        track_id)
        else:
            if not song_matches_row(song, row):
                return None, STATUS_UNMATCHED
            return song, None

    # Rows stored before track_id was reliable, or a rescanned library that
    # issued the file a new id.
    song = subsonic.search_song(title=row["title"], artist=row["artist"],
                                album=row["album"])
    if song is None:
        return None, STATUS_MISSING
    return song, None


def diagnose(rows, subsonic, lidarr):
    """Bucket every queued song by why it cannot be reaped.

    The buckets need different fixes, so lumping them together as "unmatched"
    hides the answer: an artist Lidarr has never heard of can only be handled by
    deleting the file directly, whereas a file whose path merely disagrees is a
    matching bug worth fixing here.
    """
    buckets = {}
    examples = {}

    def record(bucket, detail):
        buckets[bucket] = buckets.get(bucket, 0) + 1
        examples.setdefault(bucket, []).append(detail)

    for row in rows:
        label = f"{row['title']} — {row['artist']}"
        try:
            song, refusal = resolve_song(subsonic, row)
        except Exception as exc:
            record("error-resolving", f"{label}: {exc}")
            continue
        if song is None:
            record("not-in-navidrome" if refusal == STATUS_MISSING
                   else "navidrome-ambiguous", label)
            continue

        path = song.get("path") or ""
        artist = song.get("artist") or row["artist"]
        album_artist = song.get("albumArtist")
        names = {_norm(n) for n in (artist, album_artist) if n}
        lidarr_artists = [a for a in lidarr.artists()
                          if _norm(a.get("artistName")) in names]
        if not lidarr_artists:
            record("artist-not-in-lidarr", f"{album_artist or artist}")
            continue

        nd_album = _norm(song.get("album"))
        matching_albums = [al for al in lidarr.albums(lidarr_artists[0]["id"])
                           if _norm(al.get("title")) == nd_album]
        if not matching_albums:
            record("album-not-in-lidarr", f"{artist} — {song.get('album')}")
            continue

        if lidarr.find_track_file(path=path, artist=artist,
                                  album_artist=album_artist) is None:
            have = [tf.get("path") for tf in lidarr.track_files(lidarr_artists[0]["id"])
                    if _norm(song.get("album")) in _norm(tf.get("path"))]
            record("file-path-mismatch",
                   f"navidrome: {path}\n        lidarr:    "
                   + (have[0] if have else "(album tracked, but no files)"))
            continue
        record("would-match", label)

    total = sum(buckets.values())
    logger.info("")
    logger.info("=== why %d queued song(s) cannot be reaped ===", total)
    for bucket, count in sorted(buckets.items(), key=lambda kv: -kv[1]):
        logger.info("%-22s %4d  (%d%%)", bucket, count, round(100 * count / total))
    for bucket, _ in sorted(buckets.items(), key=lambda kv: -kv[1]):
        logger.info("")
        logger.info("%s, e.g.:", bucket)
        for detail in sorted(set(examples[bucket]))[:5]:
            logger.info("    %s", detail)
    return 0


def in_managed_library(navidrome_path: str, navidrome_root: str) -> bool:
    """Is this song in the navidrome library this job is responsible for?

    Navidrome can serve several music folders. Checking the boundary here rather
    than only in the disk fallback keeps a library wholly in or wholly out: the
    alternative splits it by the accident of which tracks Lidarr happens to know,
    deleting some and unmonitoring their albums while skipping the rest.

    With no navidrome_root configured there is only one library as far as we know,
    so everything is in scope.
    """
    if not navidrome_root or not navidrome_path.startswith("/"):
        return True
    prefix = "/" + navidrome_root.strip("/")
    return navidrome_path == prefix or navidrome_path.startswith(prefix + "/")


def looks_synthesized(song, basename: str) -> bool:
    """True if Navidrome built this filename from tags instead of reading it.

    With Subsonic.DefaultReportRealPath off (the default), Navidrome reports
    "<disc:02>-<track:02> - <Title>.<ext>" rather than the real filename. That is
    worth detecting precisely: it is a one-setting fix, and the alternative is
    inferring it from a pile of files that appear to be missing.
    """
    title, suffix = song.get("title"), song.get("suffix")
    if not title or not suffix:
        return False
    disc = song.get("discNumber") or 1
    track = song.get("track") or 0
    try:
        expected = f"{int(disc):02d}-{int(track):02d} - {title}.{suffix}"
    except (TypeError, ValueError):
        return False
    return _norm(basename) == _norm(expected)


def check_liked_root(liked_root: str, disk_root: str) -> str | None:
    """Why liked_root is unusable, or None if it is fine.

    It must not overlap the library: inside it, Navidrome would scan the kept
    songs and play them again, defeating the point; containing it, the next
    reap could find kept songs as library files.
    """
    liked = Path(liked_root)
    if not liked.is_dir():
        return f"{liked_root!r} is not a directory on this host"
    if not disk_root:
        return None
    liked, music = liked.resolve(), Path(disk_root).resolve()
    if liked == music or music in liked.parents or liked in music.parents:
        return (f"{liked} overlaps music_root {music}; it must be outside "
                "every library Navidrome scans")
    return None


def resolve_disk_path(disk_root: str, navidrome_path: str,
                      navidrome_root: str = "") -> Path | None:
    """Turn Navidrome's path into an absolute file to delete, or None to refuse.

    Navidrome usually sees the library under a different prefix than this host
    does -- `/music` inside its container versus `/plex/music` here -- so
    `navidrome_root` is stripped before the remainder is joined onto
    `disk_root`. A path that is already relative is joined as-is.

    The result must stay inside `disk_root`: the path is library data, and a
    stray `..` must never let a deletion escape into the rest of the filesystem.
    """
    root = Path(disk_root).resolve()
    navidrome_path = navidrome_path.replace("\\", "/")

    if navidrome_root:
        prefix = "/" + navidrome_root.strip("/")
        if navidrome_path == prefix or navidrome_path.startswith(prefix + "/"):
            navidrome_path = navidrome_path[len(prefix):]
        elif navidrome_path.startswith("/"):
            # Normally caught earlier by in_managed_library(); kept as a backstop
            # so this function is safe to call on its own.
            logger.debug("%s is outside the managed library %r",
                         navidrome_path, navidrome_root)
            return None

    components = [c for c in navidrome_path.split("/") if c]
    if any(component == ".." for component in components):
        logger.warning("REFUSING path with a parent reference: %s", navidrome_path)
        return None

    # After stripping the navidrome prefix the remainder is always relative to
    # disk_root; an absolute leftover means no prefix was configured for it.
    candidate = Path(navidrome_path)
    target = Path(os.path.normpath(
        candidate if candidate.is_absolute() and not navidrome_root
        else root.joinpath(*components)))
    if root != target and root not in target.parents:
        logger.warning("REFUSING %s: outside the configured music root %s",
                       target, root)
        return None

    # The check above is textual, which a symlinked directory walks straight
    # through: if <root>/Artist/Album is a link to /etc, then
    # <root>/Artist/Album/passwd still *looks* like it is inside the root.
    # Resolve the containing directory and require the real location to be
    # inside the root as well.
    parent = target.parent.resolve()
    if parent != root and root not in parent.parents:
        logger.warning("REFUSING %s: its directory resolves to %s, outside %s",
                       target, parent, root)
        return None

    # Return the resolved location, but do NOT resolve the filename itself: if
    # the track is a symlink we want to unlink the link inside the library, not
    # whatever it points at.
    return parent / target.name


def disk_file(path, song, label, *, disk_root, navidrome_root=""):
    """Find the regular file on this host behind Navidrome's path.

    Returns (Path, None), or (None, outcome) where outcome is a status or one of
    the SUSPECT_MISSING / SYNTHESIZED_PATH sentinels.
    """
    target = resolve_disk_path(disk_root, path, navidrome_root)
    if target is None:
        return None, STATUS_UNMATCHED

    if not target.exists() and not target.is_symlink():
        # A missing file only means "already deleted" if the library around it is
        # actually there. If the album directory is missing too, the tree is not
        # mounted or music_root is wrong, and concluding the song was deleted
        # would terminally write off songs whose files are fine.
        if looks_synthesized(song, target.name):
            logger.error(
                "%s: navidrome reported %r, which is a filename built from tags, "
                "not the real one. Set ND_SUBSONIC_DEFAULTREPORTREALPATH=true (or "
                "enable 'Report Real Path' for the %s player in navidrome's "
                "Players settings) and run again.",
                label, target.name, subsonic_api.CLIENT_NAME)
            return None, SYNTHESIZED_PATH
        if not target.parent.is_dir():
            logger.warning("SKIP  %s: %s does not exist — is music_root right and "
                           "the library mounted?", label, target.parent)
        else:
            logger.info("SKIP  %s: not on disk at %s", label, target)
        return None, SUSPECT_MISSING
    if target.is_dir():
        logger.warning("REFUSING %s: %s is a directory", label, target)
        return None, STATUS_UNMATCHED
    return target, None


def delete_from_disk(path, song, label, *, disk_root, apply_changes,
                     navidrome_root=""):
    """Delete a file Lidarr does not track. Returns (status, path).

    Nothing needs unmonitoring here: Lidarr has no record of this file, so it
    will not re-download it either.
    """
    if not disk_root:
        # Deliberately NOT terminal. This is a fact about the configuration, not
        # about the song: marking it would silently drop every such song from the
        # queue, and they would need --retry unmatched to come back. Leaving the
        # status unset keeps them queued for when music_root is set.
        # Reported once at startup rather than once per song.
        logger.debug("SKIP  %s: not in lidarr and no music_root configured", label)
        return None, None

    target, problem = disk_file(path, song, label, disk_root=disk_root,
                                navidrome_root=navidrome_root)
    if target is None:
        return problem, None

    if not apply_changes:
        logger.info("WOULD delete from disk %s", target)
        logger.info("      (not in lidarr, so nothing to unmonitor)")
        return WOULD_ACT, None

    os.unlink(target)
    logger.info("DELETED from disk %s", target)
    return STATUS_DELETED_DISK, str(target)


def _verified_album(lidarr, album_id, matched_artist, song):
    """Fetch the album we are about to unmonitor and prove it is the right one.

    Returns the album, or None to refuse. Lidarr derives albumId from the track
    file so this should always agree; it is checked anyway because the blast
    radius of unmonitoring is a whole album belonging to a whole artist, and a
    cheap GET is worth more than the assumption.
    """
    if not album_id:
        return None
    album = lidarr.album(album_id)
    if album.get("artistId") != matched_artist.get("id"):
        logger.warning(
            "REFUSING to unmonitor album %s: it belongs to lidarr artist %s, "
            "but the track file belongs to %s (%s)",
            album_id, album.get("artistId"), matched_artist.get("id"),
            matched_artist.get("artistName"),
        )
        return None
    navidrome_album = song.get("album")
    if navidrome_album and _loose(album.get("title")) != _loose(navidrome_album):
        # Not fatal. The identity of the album is established by the track file
        # itself -- lidarr gave us this albumId for this file, and the artist is
        # confirmed above -- so a title spelled differently in two catalogues is
        # a naming difference, not evidence of the wrong album. Worth seeing in
        # the log, not worth skipping a deletion over.
        logger.warning(
            "album %s is titled %r in lidarr but %r in navidrome; proceeding "
            "because the track file and artist both check out",
            album_id, album.get("title"), navidrome_album,
        )
    return album


class Found(NamedTuple):
    """A rated song identified in Navidrome, and in Lidarr when it tracks it."""
    label: str
    song: dict
    path: str                       # as navidrome reports it
    track_file: dict | None = None  # None: lidarr does not track this file
    artist: dict | None = None      # the lidarr artist the track file belongs to
    album: dict | None = None       # verified; the album that will be unmonitored


def locate(row, subsonic, lidarr, *, navidrome_root=""):
    """Identify the file behind a rated row, in Navidrome and then Lidarr.

    Returns (Found, None), or (None, status) when the song must be left alone.
    Raises on transient failures.
    """
    label = f"{row['title'] or '?'} — {row['artist'] or '?'}"

    song, refusal = resolve_song(subsonic, row)
    if song is None:
        if refusal == STATUS_MISSING:
            logger.info("SKIP  %s: not in navidrome (already removed?)", label)
        else:
            logger.warning("SKIP  %s: could not identify the track with confidence",
                           label)
        return None, refusal

    path = song.get("path")
    if not path:
        logger.warning("SKIP  %s: navidrome returned no path", label)
        return None, STATUS_UNMATCHED

    if not in_managed_library(path, navidrome_root):
        # Checked before the lidarr lookup, so an out-of-scope library is skipped
        # whole rather than partly deleted via whichever route happens to reach it.
        logger.info("SKIP  %s: in another navidrome library (%s)", label, path)
        return None, STATUS_OUT_OF_SCOPE

    match = lidarr.find_track_file(
        path=path,
        artist=song.get("artist") or row["artist"],
        album_artist=song.get("albumArtist"),
    )
    if match is None:
        # Lidarr manages only part of most libraries; anything it has never
        # imported can still be handled straight on disk. The path came from
        # Navidrome's record for a song we already verified against the rated
        # row, so it identifies the file directly rather than by fuzzy matching.
        logger.debug("%s: not in lidarr; falling back to disk", label)
        return Found(label, song, path), None
    track_file, matched_artist = match

    # Confirm the album before touching anything: unmonitoring is the one action
    # here that affects music we never rated, so it gets its own check rather
    # than riding on the file match.
    album_id = track_file.get("albumId")
    album = _verified_album(lidarr, album_id, matched_artist, song)
    if album_id and album is None:
        logger.warning("SKIP  %s: could not confirm the album to unmonitor", label)
        return None, STATUS_UNMATCHED
    return Found(label, song, path, track_file, matched_artist, album), None


def _unmonitor(lidarr, found, unmonitored, verb):
    """Unmonitor the album of a file that has just left the library.

    The file is gone either way; a failure to unmonitor must not lose that fact,
    so it is logged loudly rather than raised.
    """
    album_id = found.track_file.get("albumId")
    if not album_id or album_id in unmonitored:
        return
    try:
        lidarr.set_album_monitored(album_id, False)
        unmonitored.add(album_id)
        logger.info("UNMONITORED %r by %r (album %s)",
                    (found.album or {}).get("title"),
                    found.artist.get("artistName"), album_id)
    except lidarr_api.LidarrError:
        logger.exception(
            "%s %s but FAILED to unmonitor album %s — lidarr may re-download it",
            verb, found.label, album_id,
        )


def reap_song(found, lidarr, *, apply_changes, unmonitored, disk_root=None,
              navidrome_root=""):
    """Delete a disliked song. Returns (status, path removed)."""
    if found.track_file is None:
        return delete_from_disk(found.path, found.song, found.label,
                                disk_root=disk_root, apply_changes=apply_changes,
                                navidrome_root=navidrome_root)
    track_file = found.track_file

    if not apply_changes:
        logger.info("WOULD delete %s", track_file.get("path"))
        logger.info("      and unmonitor %r by %r (album %s)",
                    (found.album or {}).get("title"),
                    found.artist.get("artistName"), track_file.get("albumId"))
        return WOULD_ACT, None

    lidarr.delete_track_file(track_file["id"])
    logger.info("DELETED %s (trackFile %s)", track_file.get("path"), track_file["id"])
    _unmonitor(lidarr, found, unmonitored, "deleted")
    return STATUS_DELETED, track_file.get("path")


def liked_destination(source: Path, disk_root: str, liked_root: str) -> Path:
    """Where a liked file goes: the same Artist/Album/file layout, under
    liked_root. `source` came from disk_file(), so it is already inside the
    resolved music root and the relative part cannot climb out."""
    relative = source.relative_to(Path(disk_root).resolve())
    return Path(liked_root).resolve() / relative


def _copy_verified(source: Path, dest: Path) -> None:
    """Copy source to dest so dest only ever appears complete.

    Written under a hidden temporary name and renamed into place, so a crash
    mid-copy leaves no truncated file that a later run would mistake for done.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(f".{dest.name}.partial")
    shutil.copy2(source, partial)
    if partial.stat().st_size != source.stat().st_size:
        partial.unlink()
        raise OSError(f"copy of {source} is the wrong size; not keeping it")
    os.replace(partial, dest)


def retained_ids(lidarr, found) -> dict:
    """What is needed to put a kept song back under Lidarr later.

    Lidarr adds artists and albums by MusicBrainz id, so those are the durable
    keys; Lidarr's own album id is kept too, but it only means something to
    this Lidarr instance. Navidrome's tag-read recording id stands in when
    Lidarr does not track the file.
    """
    ids = {"mb_recording_id": found.song.get("musicBrainzId") or None}
    if found.track_file is None:
        return ids
    album_id = found.track_file.get("albumId")
    ids["lidarr_album_id"] = album_id
    ids["mb_artist_id"] = found.artist.get("foreignArtistId")
    ids["mb_album_id"] = (found.album or {}).get("foreignAlbumId")
    if album_id:
        for track in lidarr.tracks(album_id):
            if track.get("trackFileId") == found.track_file["id"]:
                ids["mb_recording_id"] = (track.get("foreignRecordingId")
                                          or ids["mb_recording_id"])
                break
    return ids


def keep_song(found, lidarr, *, apply_changes, unmonitored, disk_root,
              liked_root, navidrome_root=""):
    """Move a liked song out of the library into liked_root.

    Returns (status, path removed, retained ids).

    The copy is made and checked *before* the original is removed, so at no
    point does the only copy of a song you like depend on a step that has not
    happened yet. The original is then removed through Lidarr when it tracks
    the file, exactly like a deletion, and the album unmonitored -- otherwise
    Lidarr would see a missing track and fetch it straight back.
    """
    label = found.label
    source, problem = disk_file(found.path, found.song, label,
                                disk_root=disk_root, navidrome_root=navidrome_root)
    if source is None:
        return problem, None, {}

    track_file = found.track_file
    if track_file is not None:
        # The path match got us here; the size proves the file we are about to
        # copy is the very one lidarr is about to delete, not a namesake.
        expected = track_file.get("size")
        if expected is not None and expected != source.stat().st_size:
            logger.warning("REFUSING %s: %s is %d bytes but lidarr's file is %d",
                           label, source, source.stat().st_size, expected)
            return STATUS_UNMATCHED, None, {}

    dest = liked_destination(source, disk_root, liked_root)
    resumed = False
    if dest.exists() or dest.is_symlink():
        # liked_root is ours alone, so a same-size file at the same relative path
        # is this song from a run that stopped after copying. Anything else is a
        # collision, and overwriting it could destroy a song kept earlier.
        if dest.is_file() and dest.stat().st_size == source.stat().st_size:
            resumed = True
        else:
            logger.warning("REFUSING %s: %s already exists and differs", label, dest)
            return STATUS_UNMATCHED, None, {}

    ids = retained_ids(lidarr, found)

    if not apply_changes:
        logger.info("WOULD move %s", source)
        logger.info("      to   %s", dest)
        if track_file is not None:
            logger.info("      and unmonitor %r by %r (album %s)",
                        (found.album or {}).get("title"),
                        found.artist.get("artistName"), track_file.get("albumId"))
        else:
            logger.info("      (not in lidarr, so nothing to unmonitor)")
        return WOULD_ACT, None, {}

    if not resumed:
        _copy_verified(source, dest)
    logger.info("COPIED %s -> %s", source, dest)

    if track_file is None:
        os.unlink(source)
        logger.info("KEPT  %s (removed from library on disk)", label)
        return STATUS_KEPT_DISK, str(source), {"kept_path": str(dest), **ids}

    try:
        lidarr.delete_track_file(track_file["id"])
    except Exception:
        # The original is still in place, so drop the copy: the next run starts
        # clean rather than finding a stray duplicate in liked_root.
        dest.unlink(missing_ok=True)
        raise
    logger.info("KEPT  %s (trackFile %s removed via lidarr)", label, track_file["id"])
    _unmonitor(lidarr, found, unmonitored, "kept")
    return STATUS_KEPT, track_file.get("path"), {"kept_path": str(dest), **ids}


def process(row, subsonic, lidarr, *, apply_changes, unmonitored, disk_root=None,
            navidrome_root="", liked_root=None):
    """Handle one rated song: delete it if disliked, move it out if liked.

    Returns (status, path, retained): a terminal status, the file removed from
    the library, and for a kept song the columns needed to restore it. Status is
    None when there is nothing terminal to record (a dry run, or a song this
    configuration cannot handle yet).

    Raises on transient failures (network, HTTP) so the caller can count the
    attempt and retry on a later run.
    """
    if row["rating"] == navidrome.RATING_LIKE and not (liked_root and disk_root):
        # Like a missing music_root for deletions: a fact about configuration,
        # so not terminal, and reported once at startup rather than per song.
        logger.debug("SKIP  %s: liked, and liked_root/music_root not configured",
                     row["title"])
        return None, None, {}

    found, refusal = locate(row, subsonic, lidarr, navidrome_root=navidrome_root)
    if found is None:
        return refusal, None, {}

    if row["rating"] == navidrome.RATING_LIKE:
        return keep_song(found, lidarr, apply_changes=apply_changes,
                         unmonitored=unmonitored, disk_root=disk_root,
                         liked_root=liked_root, navidrome_root=navidrome_root)
    status, path = reap_song(found, lidarr, apply_changes=apply_changes,
                             unmonitored=unmonitored, disk_root=disk_root,
                             navidrome_root=navidrome_root)
    return status, path, {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--disliked", action="store_true",
                        help="delete disliked songs")
    parser.add_argument("--liked", action="store_true",
                        help="move liked songs out of the library into liked_root")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete/move and unmonitor (default: dry run)")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="stop after N deletions or moves (skips do not count, so a "
                             "run of out-of-scope songs cannot consume it)")
    parser.add_argument("--retry", choices=(*RETRYABLE, "all"), metavar="STATUS",
                        help="requeue songs already recorded as unmatched or failed, "
                             "with a fresh retry budget (unmatched|failed|all)")
    parser.add_argument("--status", action="store_true",
                        help="print the reap queue and exit, changing nothing")
    parser.add_argument("--diagnose", action="store_true",
                        help="explain why queued songs cannot be matched, and exit")
    parser.add_argument("--accept-missing", action="store_true",
                        help="record files absent from disk as already-deleted, "
                             "instead of stopping to ask whether the paths are right")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not (args.liked or args.disliked or args.status or args.diagnose):
        parser.error("pass --disliked and/or --liked to choose what to reap")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    # httpx logs a line per request at INFO, which buries the actual report.
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_config()
    if args.apply and not config.getboolean("lidarr", "enabled", fallback=True):
        logger.warning("lidarr.enabled is false in config.ini; forcing a dry run")
        args.apply = False

    # Same database the listener writes to; navidrome owns where it lives.
    store = navidrome.database()

    if args.status:
        for row in store.reap_summary():
            logger.info("%-8s %-15s %d", row["rating"], row["status"], row["n"])
        return 0

    # Songs with a rating not asked for are left out entirely: untouched, still
    # queued, and (under --retry) with their recorded state kept. --diagnose with
    # neither flag looks at the whole queue.
    ratings = {rating for rating, wanted in ((navidrome.RATING_LIKE, args.liked),
                                             (navidrome.RATING_DISLIKE, args.disliked))
               if wanted} or {navidrome.RATING_LIKE, navidrome.RATING_DISLIKE}
    rows = [r for r in store.unreaped(MAX_ATTEMPTS) if r["rating"] in ratings]

    if args.retry:
        wanted = RETRYABLE if args.retry == "all" else (args.retry,)
        requeued = [r for status in wanted for r in store.reaped_with_status(status)
                    if r["rating"] in ratings]
        if requeued and args.apply:
            # Give them a fresh budget, or a row that already burned through its
            # attempts would be dropped again the moment it errored once.
            store.clear_reap_state([r["event_id"] for r in requeued])
        rows = rows + requeued

    # NB: --limit is applied in the loop below, counting deletions rather than
    # rows, so a run of skips does not consume it.

    if not rows:
        logger.info("nothing to do")
        return 0

    if args.diagnose:
        with subsonic_api.Subsonic(
            config.get("navidrome_api", "url"),
            config.get("navidrome_api", "username"),
            config.get("navidrome_api", "password"),
        ) as subsonic, lidarr_api.Lidarr(
            config.get("lidarr", "url"), config.get("lidarr", "api_key"),
        ) as lidarr:
            subsonic.ping()
            lidarr.system_status()
            logger.info("lidarr knows %d artists", len(lidarr.artists()))
            return diagnose(rows, subsonic, lidarr)

    logger.info("%d rated song(s) to process%s",
                len(rows), "" if args.apply else " (dry run — nothing will change)")

    navidrome_root = config.get("navidrome_api", "navidrome_root",
                                fallback="").strip()
    disk_root = config.get("navidrome_api", "music_root", fallback="").strip()
    if disk_root and not Path(disk_root).is_dir():
        # Fail fast rather than degrade: a typo'd root would otherwise look
        # exactly like "not configured" and quietly skip most of the queue.
        logger.error("music_root %r is not a directory on this host. Set it to "
                     "navidrome's library root as seen from here, or leave it "
                     "blank to skip songs lidarr does not track.", disk_root)
        return 2
    if not disk_root:
        logger.warning(
            "no music_root configured in [navidrome_api]: songs lidarr does not "
            "track will be left queued, not deleted. Set music_root to "
            "navidrome's library root to handle them.")

    liked_root = config.get("navidrome_api", "liked_root", fallback="").strip()
    if liked_root:
        problem = check_liked_root(liked_root, disk_root)
        if problem:
            logger.error("liked_root: %s", problem)
            return 2
    if args.liked and not (liked_root and disk_root):
        logger.warning(
            "liked_root and music_root are not both set in [navidrome_api]: liked "
            "songs will be left queued, not moved out of the library.")

    counts = {}
    failures = 0
    acted = 0
    suspect = 0
    disk_confirmed = 0
    unmonitored = set()
    with subsonic_api.Subsonic(
        config.get("navidrome_api", "url"),
        config.get("navidrome_api", "username"),
        config.get("navidrome_api", "password"),
    ) as subsonic, lidarr_api.Lidarr(
        config.get("lidarr", "url"),
        config.get("lidarr", "api_key"),
    ) as lidarr:
        # Fail fast on bad credentials rather than per-row, which would mark
        # every song unmatched for a reason that has nothing to do with them.
        subsonic.ping()
        lidarr.system_status()

        for row in rows:
            try:
                status, path, retained = process(row, subsonic, lidarr,
                                       apply_changes=args.apply,
                                       unmonitored=unmonitored,
                                       disk_root=disk_root,
                                       liked_root=liked_root,
                                       navidrome_root=navidrome_root)
            except Exception:
                failures += 1
                if not args.apply:
                    logger.exception("ERROR processing %s", row["title"])
                    continue
                # Count the attempt so a song that always fails eventually stops
                # being queued, rather than accumulating run after run.
                attempts = store.record_attempt(row["event_id"])
                if attempts >= MAX_ATTEMPTS:
                    store.mark_reaped(row["event_id"], STATUS_FAILED)
                    logger.exception(
                        "ERROR processing %s — giving up after %d attempts "
                        "(marked failed; --retry failed to requeue)",
                        row["title"], attempts,
                    )
                    counts[STATUS_FAILED] = counts.get(STATUS_FAILED, 0) + 1
                else:
                    logger.exception(
                        "ERROR processing %s — attempt %d of %d, will retry next run",
                        row["title"], attempts, MAX_ATTEMPTS,
                    )
                continue
            if status is SYNTHESIZED_PATH:
                logger.error("aborting: nothing recorded, the queue is intact.")
                return 2
            if status is SUSPECT_MISSING:
                suspect += 1
                if suspect >= SUSPECT_LIMIT and not disk_confirmed \
                        and not args.accept_missing:
                    logger.error(
                        "aborting: %d songs in a row are not at the paths navidrome "
                        "reports, and nothing has been deleted from disk this run. "
                        "Check that music_root (%r) is correct and mounted here, and "
                        "that navidrome reports REAL paths — Subsonic.DefaultReportRealPath "
                        "(ND_SUBSONIC_DEFAULTREPORTREALPATH) is false by default, which "
                        "makes it synthesize paths from tags instead. Nothing was "
                        "recorded; the queue is intact. Pass --accept-missing if "
                        "they really were deleted already.", suspect, disk_root)
                    return 2
                # Once the scheme has been shown to work, an absent file really is
                # a file someone deleted earlier.
                if disk_confirmed or args.accept_missing:
                    if args.apply:
                        store.mark_reaped(row["event_id"], STATUS_MISSING)
                    counts[STATUS_MISSING] = counts.get(STATUS_MISSING, 0) + 1
                continue
            suspect = 0
            # Any file actually found and handled on disk proves the path scheme.
            if status in (STATUS_DELETED_DISK, STATUS_KEPT, STATUS_KEPT_DISK):
                disk_confirmed += 1

            if status is WOULD_ACT:
                would = ("would-keep" if row["rating"] == navidrome.RATING_LIKE
                         else "would-delete")
                counts[would] = counts.get(would, 0) + 1
                acted += 1
            elif status is not None:
                if args.apply:
                    store.mark_reaped(row["event_id"], status, path, retained)
                counts[status] = counts.get(status, 0) + 1
                if status in ACTED:
                    acted += 1

            if args.limit and acted >= args.limit:
                logger.info("stopping: --limit %d reached (%d song(s) left queued)",
                            args.limit, len(rows) - rows.index(row) - 1)
                break

    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    logger.info("done: %s%s", summary or "no terminal outcomes",
                f", {failures} error(s)" if failures else "")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
