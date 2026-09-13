# media_tracker

Asks you in Slack how you liked something you just finished, and records the answer.

Two sources feed it:

| Source        | Ingress      | Prompt          | Answer goes to          |
|---------------|--------------|-----------------|-------------------------|
| Jellyfin      | `POST /watch`    | 1–5 stars   | YamTracker (`media_save`) |
| Navidrome     | `POST /scrobble` | 👍 / 👎     | `ratings.db` (SQLite)     |

Disliked songs are then cleaned up off-line by [`reap.py`](#reaping-disliked-songs).

Both prompts come back through the same **`POST /slack`** endpoint, which verifies
the Slack signature and routes on the button's `value`.

```
Jellyfin  ──webhook──▶ /watch  ─┐
                                ├─▶ Slack DM w/ buttons ──▶ /slack ──▶ dispatch by source
Navidrome ──plugin───▶ /scrobble ┘                                       ├─ jellyfin → YamTracker
                                                                         └─ navidrome → SQLite
```

## Button value format

`/slack` receives one opaque string per click, namespaced by source so the two
flows can share an endpoint:

```
jellyfin.<rating>.<media_id>.<media_type>     e.g. jellyfin.5.108545.tv
navidrome.<like|dislike>.<event_id>           e.g. navidrome.like.<uuid4>
```

A bare `<rating>.<media_id>.<media_type>` (no source prefix) is still accepted, so
Jellyfin prompts sent before this change keep working when clicked.

## Setup

```bash
uv sync
cp ../config.ini.example ../config.ini   # then fill it in
cd media_tracker && ../.venv/bin/python listener.py
```

`config.ini` holds live credentials and is gitignored — keep it that way.
Listener binds `127.0.0.1:5190`; put it behind your reverse proxy so Slack can
reach `/slack`, and point Slack's **Interactivity → Request URL** at it.

The `[navidrome]` section needs `plugin_shared_secret` (any random string; the
plugin must send the same one) and `db_path` (relative paths resolve against the
repo root).

## Navidrome plugin

`navidrome_plugin/` is a Go/WASM Navidrome Scrobbler plugin. It is deliberately
thin — one HTTP POST on a completed play — because a WASM plugin is sandboxed and
cannot receive a Slack click or hold state. Everything interactive lives here.

Build it (needs `git`, `tinygo` or Go 1.25+, and `zip`):

```bash
cd navidrome_plugin && ./build.sh     # clones navidrome for the PDK -> like-dislike.ndp
cp like-dislike.ndp /path/to/navidrome/plugins/
```

Then in Navidrome (0.60+, plugin system enabled): open the **Plugins** page, set

- `backend_url` → `http://127.0.0.1:5190/scrobble`
- `shared_secret` → the same value as `plugin_shared_secret` in `config.ini`

assign the plugin to the users who should be prompted (the `users` permission is
required for scrobbler plugins), and enable it.

> Plugin config lives in Navidrome's database and is edited in the web UI — **not**
> in `navidrome.toml`. Values are validated against the JSON Schema in
> `manifest.json`, so a key must be declared there before it can be set.
>
> Edit `manifest.json`'s `requiredHosts` if the listener is not on
> `127.0.0.1`/`localhost`.

## Testing without Navidrome

```bash
curl -X POST http://127.0.0.1:5190/scrobble \
  -H "X-Plugin-Secret: $(python3 -c "
from configparser import ConfigParser; c=ConfigParser(); c.read('../config.ini')
print(c.get('navidrome','plugin_shared_secret'))")" \
  -H "Content-Type: application/json" \
  -d '{"user":"tim","track_id":"t1","title":"Test Song","artist":"Tester","album":"Demos","timestamp":"2026-08-22T12:00:00Z"}'
```

You should get a Slack DM. Inspect results with
`sqlite3 ../ratings.db 'select title, rating, status from ratings;'`.

## Reaping disliked songs

`reap.py` deletes the songs you gave a 👎 and stops Lidarr from fetching them
again. It is a scheduled batch job, not part of the request path — clicking
Dislike only records the answer.

```
ratings.db (rating='dislike')
      │  track_id
      ▼
Navidrome  getSong ──▶ file path
      │                   │ match on trailing path components
      ▼                   ▼
                Lidarr trackFile ──▶ DELETE (removes the file)
                       └─ albumId ──▶ PUT album/monitor {monitored:false}
```

```bash
cd media_tracker
../.venv/bin/python reap.py            # dry run: prints what it would do
../.venv/bin/python reap.py --apply    # actually delete + unmonitor
../.venv/bin/python reap.py --apply --limit 5   # ease into it
../.venv/bin/python reap.py --status   # just show the queue
```

**Dry run is the default** — nothing is deleted, and no row is marked, unless you
pass `--apply`.

### Two deletion routes

Lidarr typically manages only the slice of a library it downloaded itself; the
rest arrived by hand, from Plex, or predates it. So there are two routes, tried
in order:

1. **Via Lidarr**, when it tracks the file: `DELETE /api/v1/trackfile/{id}`.
   Lidarr removes the file itself, keeping its database consistent (no phantom
   "file present" records until the next rescan) and honouring its recycle bin.
   The album is then unmonitored so it is not fetched again. Status `deleted`.
2. **Straight from disk**, when Lidarr has never heard of the file: the path
   Navidrome reported, joined onto `music_root`, is unlinked. Nothing is
   unmonitored because Lidarr has no record to re-download from. Status
   `deleted-on-disk`.

Route 2 needs `music_root` set to Navidrome's library root *as seen from the host
running reap.py*. Leave it blank and those songs are skipped rather than deleted.

Counter-intuitively, route 2 is the more reliable identification: the path comes
straight from Navidrome's own record for a song already verified against the
rated row, rather than being matched across two systems. What it gives up is the
Lidarr cross-check on the artist, so it is fenced in instead — see below.

Use `reap.py --diagnose` to see which route your queue actually needs. It buckets
every queued song by cause (`artist-not-in-lidarr`, `album-not-in-lidarr`,
`file-path-mismatch`, `not-in-navidrome`) and changes nothing.

The join between Navidrome and Lidarr is the **file path**: `track_id` is
Navidrome's own ID and means nothing to Lidarr. Navidrome's path may be relative
to its music folder while Lidarr's is absolute, so they are compared by trailing
path components.

### Not deleting the wrong file

Failing to match is harmless — the song is reported and left alone. A confident
wrong match destroys a file. So every check below refuses rather than guesses,
and all of them must pass before anything is deleted:

- **Three path components minimum** — the filename, its album directory, *and*
  the artist directory above it. Filename plus album is not enough:
  `Greatest Hits/01 Intro.flac` is a plausible path under any number of artists.
- **The best match must be unique.** If two files score equally, the path does
  not identify one track, and the tie is never broken arbitrarily.
- **The artist is confirmed independently of the path**, so a coincidental path
  collision cannot carry the deletion onto another artist.
- **No blind library scan.** If no Lidarr artist matches by name or by library
  folder, the song is left alone. Scanning everything would widen the search
  precisely when we are least sure who the artist is.
- **Navidrome must return the song we rated.** A `track_id` is only a pointer,
  and a rescanned library can reissue one to a different file, so the title,
  artist and album that come back are checked against what was stored.
- **The title/artist fallback requires the album too, and must be unique.** The
  same song routinely appears on a studio album, a compilation and a live
  record; title and artist alone cannot tell those recordings apart.

Comparisons are case-folded and Unicode-normalised (NFC), so a macOS-decomposed
accent still matches. That can only make two spellings of the *same* name compare
equal, never two different names.

### Not deleting outside the library

Direct deletion is fenced by the music root. Navidrome's path is library data, so
it is treated as untrusted input:

- any path containing `..` is refused outright;
- the target must sit inside `music_root`, so an absolute path pointing elsewhere
  is refused;
- the containing directory is **resolved** and must also be inside the root, so a
  symlinked album directory cannot walk the deletion out of the library — a
  textual check alone passes `<root>/Artist/Album/x` straight through when
  `Album` is a link to somewhere else;
- the filename itself is deliberately *not* resolved: a symlinked track has the
  link removed from the library, never the file it points at;
- directories are never unlinked, only regular files;
- a file that is already gone reports `missing` rather than erroring.

Empty album directories are left behind after the last track goes; nothing here
removes directories. Navidrome picks the deletion up on its next scan.

### Not unmonitoring the wrong artist

Unmonitoring is the one action here that affects music you never rated, so it
gets its own check rather than riding on the file match. Before it happens, the
album is fetched from Lidarr and must prove to be the right one: its `artistId`
has to be the artist the track file belongs to, and its title has to agree with
what Navidrome calls the album. If either fails, nothing is deleted *or*
unmonitored — the whole song is skipped.

The dry run names the album and artist it would unmonitor, so you can audit it
before turning `--apply` on:

```
WOULD delete /music/Boards of Canada/Geogaddi/03 Bad Song.flac
      and unmonitor 'Geogaddi' by 'Boards of Canada' (album 55)
```

### Unmonitoring is album-wide

Lidarr has no per-track monitoring — `monitored` exists on artists and albums
only. Disliking one track therefore unmonitors **the entire album**, so nothing
else on it will be fetched or upgraded either. This is deliberate (it is the only
thing that actually prevents a re-download), but it is the one genuinely lossy
part of the job.

### Outcomes, and why the queue stays small

Every row reaches a terminal `reap_status` and then leaves the queue for good, so
work does not pile up run after run:

| status      | meaning                                                         |
|-------------|-----------------------------------------------------------------|
| `deleted`   | removed via Lidarr, album unmonitored                            |
| `deleted-on-disk` | removed directly; Lidarr does not track it, so nothing to unmonitor |
| `missing`   | already gone from Navidrome — nothing left to delete             |
| `unmatched` | could not be identified with confidence — left alone, see above  |
| `failed`    | errored `MAX_ATTEMPTS` times; gave up rather than retry for ever |

`deleted` rows also record **`reaped_path`** — the file that was actually removed
— so there is an audit trail of what the job has deleted:

```bash
sqlite3 ../ratings.db \
  "select responded_at, reap_status, reaped_path from ratings
   where reap_status is not null order by reaped_at desc limit 20;"
```

A transient failure (network, HTTP 5xx) is not given a terminal status, so the
next run retries it — but it does increment `reap_attempts`, and after
`MAX_ATTEMPTS` (5) the song is marked `failed` and dropped from the queue.
Without that cap, anything failing for a durable reason that merely *looks*
transient would be retried on every run forever, and those accumulate. Any run
with failures exits non-zero, so the timer surfaces it in `systemctl status`.

`missing` and `unmatched` are distinct on purpose: the first means there is
nothing left to delete, the second means there might be and the job would not
risk guessing. An `unmatched` song is always logged with the reason.

`unmatched` and `failed` are both recoverable by hand once you have fixed the
underlying cause — importing the album into Lidarr properly, say:

```bash
../.venv/bin/python reap.py --apply --retry unmatched   # or: failed, all
```

That requeues them with a fresh retry budget. `reap.py --status` prints the
current counts per status and changes nothing.

If the file is deleted but unmonitoring then fails, the row is still marked
`deleted` (the file really is gone) and the failure is logged at ERROR — that is
the one case where Lidarr may re-download the song.

### Scheduling

Run it from cron on **the host that holds `ratings.db`** — the same machine as
`listener.py`. It does not need the music share mounted (deletion goes through
Lidarr's API); it needs the database, `config.ini`, and HTTP access to Lidarr and
Navidrome.

```cron
# Reap disliked songs nightly. Absolute paths only -- cron has no useful cwd or
# PATH, and both are fine here: reap.py resolves config.ini and ratings.db from
# its own location, and Python puts the script's directory on sys.path, so no
# `cd` and no PYTHONPATH are needed.
23 4 * * * /opt/scripts/.venv/bin/python /opt/scripts/media_tracker/reap.py --apply 2>&1 | logger -t media-tracker-reap
```

`crontab -e` as the account that owns `ratings.db`, then read it back with:

```bash
journalctl -t media-tracker-reap -n 50      # or: grep media-tracker-reap /var/log/syslog
```

The `| logger` matters: reap.py logs a couple of lines at INFO on **every** run,
including when there is nothing to do, and cron emails anything a job writes to
stdout or stderr. Without the pipe you get mail nightly. Pipe it to `logger` (as
above), redirect to a file you can rotate, or add `MAILTO=""` at the top of the
crontab if you want it silent — but then a genuine failure is silent too, so
prefer the log.

Note that `| logger` makes the pipeline's exit status `logger`'s, so cron will
not see a failing run. The exit code is there for interactive use and for
whatever you wire up; the ERROR lines in the log are what to alert on.

While you are still building trust in the path matching, drop `--apply` from the
cron line for a few days and read the log — it will print what it *would* have
deleted without touching anything.

### Upgrading an existing install

The new `reap_*` columns are added by an `ALTER TABLE` that runs when either
process next opens the database, and the old listener is unaffected by them:
inserts name their columns explicitly, `reap_attempts` has a `DEFAULT`, and reads
go through `sqlite3.Row` by name. So the listener can keep running across the
deploy — there is no required restart, and no ordering requirement between
updating the listener and first running the reaper.

### Config

```ini
[lidarr]
url: https://lidarr.wrycu.com
api_key: ...          ; Settings -> General -> Security -> API Key
enabled: true         ; false forces a dry run even with --apply

[navidrome_api]
url: https://music.wrycu.com
username: ...         ; any Navidrome user; read-only is fine
password: ...         ; sent as a salted token, never in clear
```

## Notes & limitations

- **Playlist attribution is best-effort.** Navidrome does not record which playlist
  a play came from, so the plugin reads the user's saved play queue over the
  Subsonic API and attaches a hint like `Play queue (N tracks)`. It is often empty.
  Treat it as a hint, not ground truth.
- Once you have answered for a song, replaying it does **not** prompt again (matched
  on `track_id`, falling back to title+artist). A song with only an unanswered
  prompt outstanding will be re-prompted; a second click is a no-op.
- The Navidrome flow assumes a single Slack user (`slack_user_id`), like the
  Jellyfin flow — every Navidrome user's plays are DMed to that one person.
- Not implemented: expiring stale unanswered prompts, and rate-limiting during a
  long listening session (one DM per finished song adds up).
- The reaper unmonitors whole albums, because Lidarr cannot monitor a single
  track. One disliked song on an album you otherwise like will stop that album
  being upgraded or re-fetched.
- Nothing un-does a reap. The rating stays in `ratings.db`, so re-monitoring the
  album in Lidarr and re-downloading is a manual job.
