# Configuration

Copy the tracked examples before running the application:

```bash
cp config.example.yaml config.yaml
chmod 600 config.yaml
```

`config.yaml` is the only manually maintained application configuration. It is
ignored because it may contain local paths and credentials. `~` is expanded to
the current user's home directory. Relative paths are resolved from the
directory containing the active configuration file.

The example is deliberately conservative. Start with it and change only values
needed by the deployment.

## Paths and databases

```yaml
paths:
  recordings_path: ./recordings
  recordings_fav_path: ./recordings_fav
  inactive_users_path: ./inactive_users
  log_path: ./logs

database:
  path: ./db.sqlite
```

`recordings_fav_path` is an optional symlink view. Its links use targets relative
to the favorites directory so the source and favorites directories can be moved
or archived together. `inactive_users_path` is used when a supported
user-deactivation operation moves a recording directory.

## Supervisor intervals

The `intervals` section controls pacing and periodic work:

- `min_user_interval`: delay between consecutive recorder starts;
- `default_interval` and `default_check_interval`: initial values stored for
  newly discovered users;
- `after_live_check_interval`: delay applied after a live check;
- `waf_block_pause_duration`: pause after a TikTok WAF response;
- `supervisor_heartbeat_interval`: main database heartbeat cadence.

`persistent_live_system` configures the detached process manager and health
monitor. Important values are:

- `max_live_processes` (default `20`);
- `health_check_interval` and `file_check_interval`;
- `process_restart_delay` and `max_restart_attempts`;
- graceful/forced shutdown timeouts;
- `segment_on_reconnect`;
- `no_stream_data_retry_cooldown`;
- `lock_file_path`.

Recording metadata publication is optional. If `metadata_enabled` is `true`,
both `metadata_path` and `compressed_output_path` must be configured, and
`segment_on_reconnect` must also be enabled.

## Selenium

```yaml
selenium:
  database_path: ./selenium_live.db
  refresh_interval: 60
  chrome_profile_path: ./private/selenium_chrome_profile
  cache_dir: ./tmp/chrome-cache
```

The Selenium process accepts an optional `chrome_binary_path`. Without it,
Selenium discovers Chrome or Chromium normally. Profile, cache, screenshots,
and driver logs are runtime data.

`periodic_restart_*` and `hang_*` settings bound recovery from a stalled browser
session. Screenshot capture is disabled in the example; when enabled,
`screenshots_dir` and `screenshots_retention_days` control storage and cleanup.
Activity simulation is controlled by `simulation_enabled`, `simulation_pages`,
and its minimum/maximum waits.

## Cookies

The default workflow uses the JSON file exported by the Selenium monitor:

```yaml
cookies:
  enabled: true
  cookies_source: json
  cookie_json_file: ./private/cookies_full.json
```

For Firefox extraction, use `cookies_source: firefox` and provide
`firefox_cookie_db_path`, `target_cookie_json_path`, and
`cookies_to_extract`. The database path may reference
an absolute or config-relative path. Cookie JSON is written through a securely created
same-directory temporary file and atomically replaced with owner-only
permissions. Newly created private directories are owner-only; keep any
pre-existing parent directory protected as well.

Relative cookie paths are resolved from the directory containing the active
configuration file. Absolute paths are used unchanged. Pass the same config to
the monitor, supervisor, and web dashboard so every process uses the same paths
and web monitor port:

```bash
uv run python scripts/selenium_live_monitor.py --config /etc/tklivetracker/config.yaml
uv run python selenium_supervisor.py --config /etc/tklivetracker/config.yaml --server
uv run python web_monitor/app.py --config /etc/tklivetracker/config.yaml
```

Use the supervisor/recorder `--no-cookies` option only when deliberately
operating without an authenticated session.

## Telegram

Supervisor Telegram notifications are disabled by default:

```yaml
telegram:
  notifications:
    enabled: true
    bot_token: "YOUR_BOT_TOKEN"
    chat_id: "YOUR_CHAT_ID"
```

Never place real tokens or chat IDs in tracked files.

The recorder's separate `-telegram` option uploads a completed recording using
Pyrogram. Install this optional feature before using the flag:

```bash
uv sync --frozen --extra telegram-upload
```

It needs an API ID/hash in addition to the bot credentials:

```yaml
telegram:
  upload:
    api_id: "YOUR_API_ID"
    api_hash: "YOUR_API_HASH"
    bot_token: "YOUR_BOT_TOKEN"
    chat_id: "YOUR_CHAT_ID"
    session_path: ./private
```

Keep `config.yaml` and generated Pyrogram sessions owner-readable only (mode
`0600` on POSIX). The whole `private/` directory is ignored and should be mode
`0700`. Recorder upload remains optional and independent of supervisor alerts.

## Web monitor

```yaml
web_monitor:
  host: 0.0.0.0
  port: 5001
  allowed_origins: []
```

`config.yaml` is the primary source. Use `--config` to select another file;
relative database and recording paths are resolved from that file's directory.
`--host` and `--port` override it for one run. Missing keys retain the
compatibility defaults `0.0.0.0:5001`.

Binding to `0.0.0.0` makes the dashboard reachable through every network
interface allowed by the host firewall, including a trusted LAN. The dashboard
has no authentication. `--read-only` rejects application mutations but neither
hides data nor authenticates clients. Use firewall, VPN, TLS, and authenticated
reverse-proxy controls appropriate to the deployment.

Unsafe browser requests accept the direct request origin. When a trusted HTTPS
reverse proxy changes the browser-facing origin, list exact external origins:

```yaml
web_monitor:
  allowed_origins:
    - https://tracker.example.com
```

Do not add paths or wildcards. `X-Forwarded-*` headers are ignored, so an
untrusted client cannot opt itself into the allowed set.
