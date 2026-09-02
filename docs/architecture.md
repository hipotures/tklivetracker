# Architecture

TkLiveTracker separates detection, orchestration, recording, and presentation
so a browser or supervisor restart does not automatically terminate active
recordings.

```text
scripts/selenium_live_monitor.py
        │ writes live observations and viewer peaks
        ▼
selenium_live.db
        │ read by
        ▼
selenium_supervisor.py ── starts ──▶ python -m recorder.main
        │                              │
        │ coordinates                  ├─ writes the media stream directly
        ▼                              ├─ writes recording metadata
db.sqlite ◀────────────────────────────└─ updates live-session state
        │
        ▼
web_monitor/app.py
```

## Selenium live monitor

`scripts/selenium_live_monitor.py` is the only maintained component that owns a
Chrome/Chromium WebDriver. It uses a persistent profile under `private/`,
inspects TikTok's `/live` page, exports browser cookies to the configured
`cookies.cookie_json_file`, and stores observations in `selenium_live.db`.

The monitor is a separate long-running process. The supervisor does not start
Chrome and does not scrape TikTok itself.

## Supervisor

`selenium_supervisor.py` reads current live users from `selenium_live.db`, keeps
the main SQLite database synchronized, and delegates process lifecycle work to
`persistent_live_manager/`.

Recorders are detached processes with database metadata and process identity
information. Process inventory, PID fingerprints, startup metadata, and health
checks reduce duplicate starts and avoid acting on unrelated reused PIDs.
Detached recorders are intended to survive a supervisor restart.

## Recorder

The supervisor launches the integrated recorder as:

```text
python -m recorder.main -user USERNAME -mode automatic
    --output-file PATH --persistent-mode --supervisor-log-path PATH ...
```

The recorder resolves the room and stream, records media, handles reconnect
parts, and reports live-session start/stop state through `scripts/db_helper.py`.
Startup metadata lets the supervisor link a child process to the correct live
row without changing the public recorder CLI.

Recorder exit codes 0, 10, 11, and 12, its CLI shape, log markers, metadata
schemas, and graceful SIGTERM/SIGINT handling are compatibility contracts.

The retained `--ffmpeg-remux` argument is a launcher compatibility flag. The
current implementation logs that remuxing is unavailable and writes the raw
stream; FFmpeg is not required by the active recording path.

## Databases and files

- `db.sqlite` stores users, live sessions, recorder processes, and supervisor
  status.
- `selenium_live.db` stores Selenium observations separately from recorder
  lifecycle state.
- `recordings/USERNAME/` owns a user's recording output.
- `recordings_fav/` is an optional symlink view of favorite users.
- `inactive_users/` receives directories moved by supported deactivation tools.
- `private/` contains browser profiles and cookies and must never be committed.

SQLite access is local. Backups must use SQLite's online backup mechanism rather
than copying a potentially active database file.

## Web monitor

`web_monitor/app.py` is a Flask administrative dashboard. Mutating endpoints
share a server-side read-only guard, and browser-originated unsafe methods are
accepted only from the direct request origin or exact origins explicitly listed
in `web_monitor.allowed_origins`. Forwarded headers are not trusted.

The dashboard intentionally has no user/account system. Network exposure and
authentication are deployment responsibilities; `--read-only` is a mutation
control, not authentication.
