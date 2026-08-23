# media_tracker

Asks you in Slack how you liked something you just finished, and records the answer.

Two sources feed it:

| Source        | Ingress      | Prompt          | Answer goes to          |
|---------------|--------------|-----------------|-------------------------|
| Jellyfin      | `POST /watch`    | 1–5 stars   | YamTracker (`media_save`) |
| Navidrome     | `POST /scrobble` | 👍 / 👎     | `ratings.db` (SQLite)     |

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
