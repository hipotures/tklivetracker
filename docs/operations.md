# Operations

Run commands from the repository root so local configuration and module imports
resolve consistently.

## Process order

Start the Selenium monitor first because it owns live detection:

```bash
uv run python scripts/selenium_live_monitor.py --config config.yaml
```

Use visible mode only for an intentional browser-session refresh:

```bash
uv run python scripts/selenium_live_monitor.py --config config.yaml --headless-off
```

Start the supervisor after `selenium_live.db` is available:

```bash
uv run python selenium_supervisor.py --config config.yaml --server
```

The optional dashboard is a third process:

```bash
uv run python web_monitor/app.py --config config.yaml
```

Use the same `--config` path for all three processes, including the dashboard:

```bash
uv run python web_monitor/app.py --config /etc/tklivetracker/config.yaml
```

Relative cookie, database, and recording paths are anchored to that file's
directory. Supervisor callbacks to the local dashboard use `web_monitor.port`;
wildcard bind hosts such as `0.0.0.0` are contacted through the corresponding
loopback address.

## Systemd user services

Templates under `deploy/systemd/` run the same three commands as user services.
Copy them to `~/.config/systemd/user/`, replace `__INSTALL_DIR__` and
`__UV_PATH__`, and inspect them before enabling:

```bash
systemctl --user daemon-reload
systemctl --user enable --now tklivetracker.target
```

Every unit uses the same `__INSTALL_DIR__/config.yaml`. The templates contain no
credentials, do not require root, and are not installed by `setup.sh`.

## Shutdown and process inspection

SIGTERM and SIGINT request graceful shutdown. Detached recorders can survive a
supervisor restart by design; stopping the supervisor is not equivalent to
stopping every recording.

Inspect the administrative CLI before using a state-changing action:

```bash
uv run python selenium_supervisor.py --help
uv run python selenium_supervisor.py --list-processes
uv run python selenium_supervisor.py --health-check-only
uv run python selenium_supervisor.py --check-sync
```

Actions such as `--stop-all-lives`, `--restart-user`, cleanup operations,
deactivation, and `--mark-deleted-users` modify processes or database state.
Use them only after reviewing the CLI help and the displayed target set.
`--mark-deleted-users` acts only on accounts whose stored TikTok identifiers are
already marked `-1`; it does not discover deleted accounts itself.

## Database backups

Create a consistent SQLite snapshot with the supported helper:

```bash
uv run python scripts/backup_database.py
```

By default the helper reads `database.path` from `config.yaml`. `--database`
overrides it explicitly; a missing configuration is an error rather than a
silent fallback to another database. The helper uses SQLite's online backup
API, validates the snapshot with `PRAGMA integrity_check`, writes it atomically
with private permissions, and
keeps a configurable number of recent backups. It never restores or overwrites
the source database.

```bash
uv run python scripts/backup_database.py --help
uv run python scripts/backup_database.py --database ./db.sqlite --output-dir ./backup --keep 10
```

Test migrations only on a backup in a temporary directory, then run
`PRAGMA integrity_check` again after migration.

## Favorite and deactivation helpers

The optional folder-oriented helpers are installed into `~/.local/bin`:

```bash
uv run python scripts/install_tt_tools.py --help
uv run python scripts/install_tt_tools.py --config config.yaml
```

This installs `ttfav`, `ttdel`, and `fav-mtime` plus a local configuration under
`~/.config/ttracker/`. `ttfav` toggles favorite state from a configured user
folder, `ttdel` deactivates the current user and moves its recording directory
only when no active recorder is registered,
and `fav-mtime` synchronizes favorite symlink timestamps. Use each command's
`--dry-run` option before a state-changing invocation.

## Release/development checks

```bash
./scripts/check.sh
```

The check helper runs the complete offline pytest suite and compiles maintained
Python sources. It does not start Chrome, TikTok requests, recorders, or servers.

## Runtime files

Expected local data includes:

- `config.yaml`;
- `db.sqlite` and `selenium_live.db` (including WAL/SHM files);
- `private/` cookies and Chrome profile;
- `recordings/`, `recordings_fav/`, and `inactive_users/`;
- `logs/`, `backup/`, and `tmp/`;
- `supervisor.lock` and recorder metadata/logs.

These files are ignored and must not be published. Do not delete stale-looking
PID, metadata, or recording files without first proving that no live recorder
owns them.
