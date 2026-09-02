# TkLiveTracker

TkLiveTracker is a self-hosted application that detects TikTok live streams,
starts independent recording processes, tracks recording metadata in SQLite,
and provides a Flask dashboard for administration and history.

The project is independent and is not affiliated with or endorsed by TikTok or
ByteDance.

## How it works

TkLiveTracker has three long-running components:

1. `scripts/selenium_live_monitor.py` uses Chrome or Chromium to inspect
   TikTok's live page, stores observations in `selenium_live.db`, and exports
   browser cookies for the recorder.
2. `selenium_supervisor.py` reads the Selenium database and manages detached
   `python -m recorder.main` processes. Recorders can continue while the
   supervisor restarts.
3. `web_monitor/app.py` serves the dashboard and reads the main SQLite database.

The integrated recorder obtains stream metadata, writes stream media directly,
updates the live-session database, and publishes optional recording metadata.
Telegram notifications and the recorder's optional Telegram upload flag remain
available when explicitly configured.

See [Architecture](docs/architecture.md) for the component and lifecycle model.

## Requirements

- Linux and Git
- [uv](https://docs.astral.sh/uv/)
- Chrome or Chromium; Selenium Manager supplies a compatible driver when the
  platform supports it
- Linux is the maintained deployment environment. Process inspection,
  `/proc`, signals, detached processes, and symlink-based favorite views are
  Linux-oriented.

The repository's `.python-version` and uv manage Python 3.12+. TikTok behavior
and endpoints change independently of this project. No live
service compatibility guarantee is implied.

## Quick Start

```bash
git clone https://github.com/hipotures/tklivetracker.git
cd tklivetracker
./scripts/setup.sh
```

`setup.sh` installs locked runtime dependencies, creates `config.yaml` only if
missing, protects private paths, and runs an offline doctor. Edit `config.yaml`
if needed. It may contain credentials and must not be committed.

The supplied examples use relative local paths and contain no credentials.
Review [Configuration](docs/configuration.md) before enabling
Telegram, recording metadata, or remote dashboard access.

### Browser session and cookies

The Selenium monitor uses a persistent Chrome profile under `private/` and
exports cookies to `cookies.cookie_json_file` (the example uses
`private/cookies_full.json`). Relative filesystem paths are resolved from the
directory containing the active configuration file. Start it visibly once to sign in
or refresh the session:

```bash
uv run python scripts/selenium_live_monitor.py --config config.yaml --headless-off
```

When Chrome opens, log into TikTok. Once login is complete, return to the
terminal and press Enter. Chrome closes automatically and the persistent
profile is retained. Then start normal operation.

Cookie files provide account access. Keep them private, never attach them to an
issue, and never commit them.

## Run and deploy

Run the components from the repository root in separate terminals. Start the
Selenium monitor first:

```bash
uv run python scripts/selenium_live_monitor.py --config config.yaml
```

Then start the recording supervisor:

```bash
uv run python selenium_supervisor.py --config config.yaml --server
```

Finally, start the dashboard if wanted:

```bash
uv run python web_monitor/app.py --config config.yaml
```

The web monitor reads its bind address from the selected configuration file:

```yaml
web_monitor:
  host: 0.0.0.0
  port: 5001
  allowed_origins: []
```

`0.0.0.0` listens on every interface reachable through the host's firewall and
allows access from trusted LAN devices such as phones. CLI `--host` and
`--port` values override configuration for one invocation.

The dashboard has no built-in authentication. `--read-only` blocks
server-side application mutations, but it does not hide data and is not
authentication. Use firewall rules, a VPN, or an authenticated reverse proxy
before exposing it beyond a trusted LAN.

For an HTTPS reverse proxy, add each exact browser-facing origin to
`web_monitor.allowed_origins`. Forwarded headers are not trusted by default.

```bash
uv run python web_monitor/app.py --config config.yaml --read-only
uv run python web_monitor/app.py --config config.yaml --host 127.0.0.1 --port 5001
```

Operational commands, backups, shutdown behavior, and optional helper tools are
documented in [Operations](docs/operations.md). Inspect a CLI without starting
the application with:

```bash
uv run python selenium_supervisor.py --help
uv run python scripts/selenium_live_monitor.py --help
uv run python web_monitor/app.py --help
uv run python -m recorder.main --help
```

Optional systemd user-service templates are available under `deploy/systemd/`.
Setup does not install or enable them. See [Operations](docs/operations.md#systemd-user-services).

Recorder upload to Telegram is optional. Install its additional dependencies
only when needed:

```bash
uv sync --frozen --extra telegram-upload
```

Supervisor Telegram alerts do not require this extra. See
[Configuration](docs/configuration.md#telegram).

## Testing

Install the development group and run the deterministic offline checks:

```bash
uv sync --frozen --group dev
uv run pytest -q
./scripts/check.sh
```

Tests use temporary databases, directories, mocked HTTP responses, and fake
processes. They must not use real TikTok sessions or production resources.

## Project structure

```text
selenium_supervisor.py       recording supervisor and administrative CLI
scripts/selenium_live_monitor.py
                             Selenium detection process
recorder/                    integrated recorder and stream handling
persistent_live_manager/    detached process lifecycle and health monitoring
modules/                     SQLite and domain helpers
web_monitor/                 Flask dashboard
utils/                       shared validation, path, and process helpers
scripts/                     supported operational/developer commands
tests/                       deterministic pytest suite
```

## Recorder exit codes

The supervisor depends on these `recorder.main` exit codes:

| Code | Meaning |
| ---: | --- |
| 0 | Normal successful completion |
| 10 | No room ID is available; the user may never have been live |
| 11 | The user is not currently live |
| 12 | No stream media was obtained (`NO_STREAM_DATA`) |

The module name, persistent-process arguments, metadata protocol, and these
exit codes are compatibility contracts.

## Security

See [SECURITY.md](SECURITY.md) for private vulnerability reporting and the web
monitor's security boundary. Do not publish credentials, cookies, browser
profiles, session databases, private network details, or production paths in
issues or pull requests.

## License and third-party code

TkLiveTracker is released under the [MIT License](LICENSE).

Portions of `recorder/` were originally derived from Michele's
[TikTok Live Recorder](https://github.com/Michele0303/tiktok-live-recorder),
also under the MIT License. That implementation has since been substantially
modified and integrated here; the upstream project is not a separate runtime
dependency and this repository is not represented as synchronized with it.
See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the complete notice and
upstream license text.
