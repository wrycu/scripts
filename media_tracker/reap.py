#!/usr/bin/env python3
"""Delete songs you disliked in Slack, and stop Lidarr from fetching them again.

Run on a schedule. For every `dislike` in ratings.db that has not been dealt
with yet:

  1. resolve the Navidrome track_id to a file path (Subsonic `getSong`),
  2. find the matching Lidarr track file by path,
  3. delete it through Lidarr, and unmonitor the album so it is not re-grabbed.

Deleting through Lidarr rather than with os.remove keeps Lidarr's database
honest and means this can run somewhere that has no access to the music share.

Lidarr has no per-track monitoring, so step 3 unmonitors the *whole album* —
nothing else on that album will be fetched or upgraded afterwards.

Dry run by default; pass --apply to actually delete anything.

    python reap.py                    # show what would happen
    python reap.py --apply            # do it
    python reap.py --apply --limit 5  # do it, but cautiously
"""
import argparse
import logging
import os
import sys
import unicodedata
from pathlib import Path

import lidarr as lidarr_api
import navidrome
import subsonic as subsonic_api
from helpers import load_config

logger = logging.getLogger("reap")

# reap_status values. Only TRANSIENT failures are left unmarked, so they are
# retried on the next run; everything else is terminal and recorded.
STATUS_DELETED = "deleted"      # removed via lidarr, album unmonitored
STATUS_DELETED_DISK = "deleted-on-disk"  # removed directly; lidarr does not track it
STATUS_MISSING = "missing"      # already gone from Navidrome; nothing to delete
STATUS_UNMATCHED = "unmatched"  # Navidrome knows it, Lidarr does not
STATUS_FAILED = "failed"        # kept erroring; gave up after MAX_ATTEMPTS

RETRYABLE = (STATUS_UNMATCHED, STATUS_FAILED)

# How many times a song may fail transiently before we stop queueing it. Without
# a cap, a song that fails for a durable reason that merely looks transient is
# retried on every run for ever, and those pile up.
MAX_ATTEMPTS = 5


def _norm(value):
    return unicodedata.normalize("NFC", (value or "").strip()).casefold()


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


def resolve_disk_path(disk_root: str, navidrome_path: str) -> Path | None:
    """Turn Navidrome's path into an absolute file to delete, or None to refuse.

    Navidrome reports paths relative to its music folder, so they are joined onto
    the configured root. The result must stay inside that root: the path is
    library data, and a stray `..` must never let a deletion escape into the rest
    of the filesystem.
    """
    root = Path(disk_root).resolve()
    components = [c for c in navidrome_path.replace("\\", "/").split("/") if c]
    if any(component == ".." for component in components):
        logger.warning("REFUSING path with a parent reference: %s", navidrome_path)
        return None

    candidate = Path(navidrome_path)
    target = Path(os.path.normpath(
        candidate if candidate.is_absolute() else root.joinpath(*components)))
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


def delete_from_disk(path, song, label, *, disk_root, apply_changes):
    """Delete a file Lidarr does not track. Returns (status, path).

    Nothing needs unmonitoring here: Lidarr has no record of this file, so it
    will not re-download it either.
    """
    if not disk_root:
        logger.warning("SKIP  %s: not in lidarr, and no music_root is configured",
                       label)
        return STATUS_UNMATCHED, None

    target = resolve_disk_path(disk_root, path)
    if target is None:
        return STATUS_UNMATCHED, None

    if not target.exists() and not target.is_symlink():
        logger.info("SKIP  %s: already gone from disk (%s)", label, target)
        return STATUS_MISSING, None
    if target.is_dir():
        logger.warning("REFUSING %s: %s is a directory", label, target)
        return STATUS_UNMATCHED, None

    if not apply_changes:
        logger.info("WOULD delete from disk %s", target)
        logger.info("      (not in lidarr, so nothing to unmonitor)")
        return None, None

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
    if navidrome_album and _norm(album.get("title")) != _norm(navidrome_album):
        logger.warning(
            "REFUSING to unmonitor album %s: lidarr calls it %r, navidrome %r",
            album_id, album.get("title"), navidrome_album,
        )
        return None
    return album


def process(row, subsonic, lidarr, *, apply_changes, unmonitored, disk_root=None):
    """Handle one disliked song.

    Returns (status, path): a terminal status and the file removed, or
    (None, None) when there is nothing terminal to record (a dry run).

    Raises on transient failures (network, HTTP) so the caller can count the
    attempt and retry on a later run.
    """
    label = f"{row['title'] or '?'} — {row['artist'] or '?'}"

    song, refusal = resolve_song(subsonic, row)
    if song is None:
        if refusal == STATUS_MISSING:
            logger.info("SKIP  %s: not in navidrome (already deleted?)", label)
        else:
            logger.warning("SKIP  %s: could not identify the track with confidence",
                           label)
        return refusal, None

    path = song.get("path")
    if not path:
        logger.warning("SKIP  %s: navidrome returned no path", label)
        return STATUS_UNMATCHED, None

    match = lidarr.find_track_file(
        path=path,
        artist=song.get("artist") or row["artist"],
        album_artist=song.get("albumArtist"),
    )
    if match is None:
        # Lidarr manages only part of most libraries; anything it has never
        # imported can still be removed straight from disk. The path came from
        # Navidrome's record for a song we already verified against the rated
        # row, so it identifies the file directly rather than by fuzzy matching.
        logger.debug("%s: not in lidarr; falling back to disk", label)
        return delete_from_disk(path, song, label, disk_root=disk_root,
                                apply_changes=apply_changes)
    track_file, matched_artist = match

    # Confirm the album before touching anything: unmonitoring is the one action
    # here that affects music we never rated, so it gets its own check rather
    # than riding on the file match.
    album_id = track_file.get("albumId")
    album = _verified_album(lidarr, album_id, matched_artist, song)
    if album_id and album is None:
        logger.warning("SKIP  %s: could not confirm the album to unmonitor", label)
        return STATUS_UNMATCHED, None

    if not apply_changes:
        logger.info("WOULD delete %s", track_file.get("path"))
        logger.info("      and unmonitor %r by %r (album %s)",
                    (album or {}).get("title"), matched_artist.get("artistName"),
                    album_id)
        return None, None

    lidarr.delete_track_file(track_file["id"])
    logger.info("DELETED %s (trackFile %s)", track_file.get("path"), track_file["id"])

    # The file is gone either way; a failure to unmonitor must not lose that fact,
    # so it is logged loudly rather than raised.
    if album_id and album_id not in unmonitored:
        try:
            lidarr.set_album_monitored(album_id, False)
            unmonitored.add(album_id)
            logger.info("UNMONITORED %r by %r (album %s)",
                        (album or {}).get("title"),
                        matched_artist.get("artistName"), album_id)
        except lidarr_api.LidarrError:
            logger.exception(
                "deleted %s but FAILED to unmonitor album %s — lidarr may re-download it",
                label, album_id,
            )
    return STATUS_DELETED, track_file.get("path")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="actually delete and unmonitor (default: dry run)")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="process at most N songs this run")
    parser.add_argument("--retry", choices=(*RETRYABLE, "all"), metavar="STATUS",
                        help="requeue songs already recorded as unmatched or failed, "
                             "with a fresh retry budget (unmatched|failed|all)")
    parser.add_argument("--status", action="store_true",
                        help="print the reap queue and exit, changing nothing")
    parser.add_argument("--diagnose", action="store_true",
                        help="explain why queued songs cannot be matched, and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

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
            logger.info("%-10s %d", row["status"], row["n"])
        return 0

    rows = store.unreaped_dislikes(MAX_ATTEMPTS)

    if args.retry:
        wanted = RETRYABLE if args.retry == "all" else (args.retry,)
        requeued = [r for status in wanted for r in store.reaped_with_status(status)]
        if requeued and args.apply:
            # Give them a fresh budget, or a row that already burned through its
            # attempts would be dropped again the moment it errored once.
            store.clear_reap_state([r["event_id"] for r in requeued])
        rows = rows + requeued

    if args.limit:
        rows = rows[: args.limit]

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

    logger.info("%d disliked song(s) to process%s",
                len(rows), "" if args.apply else " (dry run — nothing will be deleted)")

    disk_root = config.get("navidrome_api", "music_root", fallback="").strip()
    if disk_root and not Path(disk_root).is_dir():
        logger.error("music_root %s does not exist on this host; "
                     "songs lidarr does not track will be skipped", disk_root)
        disk_root = ""
    elif not disk_root:
        logger.warning("no music_root configured; songs lidarr does not track "
                       "cannot be deleted")

    counts = {}
    failures = 0
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
                status, path = process(row, subsonic, lidarr,
                                       apply_changes=args.apply,
                                       unmonitored=unmonitored,
                                       disk_root=disk_root)
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
            if status is None:
                continue
            if args.apply:
                store.mark_reaped(row["event_id"], status, path)
            counts[status] = counts.get(status, 0) + 1

    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    logger.info("done: %s%s", summary or "no terminal outcomes",
                f", {failures} error(s)" if failures else "")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
