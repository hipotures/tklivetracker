# AGENTS.md

Use the fff MCP tools for all file search operations instead of default tools.

Guidance for coding agents and developers working in this repository.

## Safety boundaries

- Do not start, stop, restart, signal, or inspect production application
  processes during ordinary development.
- Never run `--stop-all-lives` unless the user explicitly requests that exact
  production operation.
- Do not use production `config.yaml`, databases, cookies, browser profiles,
  recordings, ports, PIDs, or `/tmp/tiktok_live_*` artifacts in tests.
- Use temporary paths/databases and mocked network/process objects. Tests are
  deterministic and offline by default.
- Use `uv`, never `pip`, for project dependency and environment operations.
- Never copy a live SQLite database with `cp`; use SQLite's online backup API
  and verify snapshots with `PRAGMA integrity_check`.

## Current architecture

Three separate processes form the application:

1. `scripts/selenium_live_monitor.py` owns Chrome/Chromium and writes
   `selenium_live.db`.
2. `selenium_supervisor.py` reads that database and manages detached
   `python -m recorder.main` subprocesses.
3. `web_monitor/app.py` serves the Flask dashboard.

The recorder is integrated source derived in part from an MIT-licensed upstream
project. Do not replace it with a package dependency or redesign its process
contract during unrelated work.

Preserve `recorder.main`, its supported CLI arguments, detached-process and
signal behavior, process inventory recognition, database start/stop protocol,
startup/recording metadata, reconnect part files, output ownership, log markers,
and exit codes 0, 10, 11, and 12.

## Development commands

```bash
uv sync --frozen --group dev
uv run pytest -q
./scripts/check.sh

uv run python selenium_supervisor.py --help
uv run python scripts/selenium_live_monitor.py --help
uv run python web_monitor/app.py --help
uv run python -m recorder.main --help
```

Do not use help validation on a CLI unless static inspection confirms that it
exits before starting browser, network, recorder, or server work.

## Configuration and runtime data

`config.example.yaml` is the safe tracked example. `config.yaml`, `private/`,
databases, logs, recordings, Chrome profiles, and generated metadata are local
and ignored. The application does not load dotenv files.

The web monitor reads `web_monitor.host` and `web_monitor.port` from
`config.yaml`; CLI values override them. It intentionally supports trusted-LAN
binding. `--read-only` disables server-side writes but is not authentication or
privacy protection.

## Change discipline

- Make the smallest compatible change and avoid unrelated refactors.
- Search imports, tests, documentation, and process command lines before
  deleting or renaming anything.
- Preserve existing safety checks around paths, symlinks, PID identity, SQLite
  lifecycle, and exact-output cleanup.
- Keep maintained prose and comments in English.
- Before committing, run relevant tests, the complete offline suite,
  compile checks, `git diff --check`, and `git status --short`.
