#!/usr/bin/env python3
"""Create a verified, atomic SQLite backup without touching the source file."""

from __future__ import annotations

import argparse
import os
import sqlite3
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import yaml

from utils.config_paths import (
    absolute_config_path,
    resolve_config_placeholders,
    resolve_path,
)


def _read_only_uri(path: Path) -> str:
    return f"file:{quote(str(path), safe='/')}?mode=ro"


def _verify_database(path: Path) -> None:
    with sqlite3.connect(_read_only_uri(path), uri=True, timeout=30.0) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise RuntimeError(f"SQLite integrity check failed for {path}")


def _prune_backups(output_dir: Path, keep: int) -> None:
    backups = sorted(
        (
            path
            for path in output_dir.glob("tklivetracker-*.sqlite")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for old_backup in backups[keep:]:
        old_backup.unlink()


def _remove_temporary_database(path: Path) -> None:
    path.unlink(missing_ok=True)
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)


def backup_database(database: Path, output_dir: Path, keep: int = 10) -> Path:
    """Back up one SQLite database and return the verified snapshot path."""
    database = database.expanduser()
    if database.is_symlink():
        raise ValueError(f"database must be a regular file: {database}")
    database = database.resolve()
    output_dir = output_dir.expanduser().resolve()

    if keep < 1:
        raise ValueError("keep must be at least 1")
    if not database.is_file() or database.is_symlink():
        raise ValueError(f"database must be a regular file: {database}")

    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    final_path = output_dir / f"tklivetracker-{timestamp}.sqlite"
    if final_path.exists() or final_path.is_symlink():
        raise FileExistsError(f"backup target already exists: {final_path}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".tklivetracker-backup-",
        suffix=".sqlite",
        dir=output_dir,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    if os.name == "posix":
        os.chmod(temporary_path, stat.S_IRUSR | stat.S_IWUSR)

    try:
        with sqlite3.connect(
            _read_only_uri(database), uri=True, timeout=30.0
        ) as source, sqlite3.connect(temporary_path, timeout=30.0) as destination:
            source.backup(destination)
            destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            destination.execute("PRAGMA journal_mode=DELETE")
        _verify_database(temporary_path)
        os.replace(temporary_path, final_path)
        if os.name == "posix":
            os.chmod(final_path, stat.S_IRUSR | stat.S_IWUSR)
        _prune_backups(output_dir, keep)
        return final_path
    except Exception:
        _remove_temporary_database(temporary_path)
        raise


def database_path_from_config(config_path: Path) -> Path:
    """Resolve database.path relative to the selected configuration file."""
    config_path = Path(absolute_config_path(config_path))
    with config_path.open("r", encoding="utf-8") as config_file:
        config = resolve_config_placeholders(yaml.safe_load(config_file) or {})
    configured = config.get("database", {}).get("path")
    if not isinstance(configured, str) or not configured.strip():
        raise ValueError(f"database.path is missing from {config_path}")
    resolved = resolve_path(configured, base_dir=str(config_path.parent))
    if not resolved:
        raise ValueError(f"database.path could not be resolved from {config_path}")
    return Path(resolved)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create an atomic, integrity-checked SQLite backup."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="Source SQLite database (overrides config.yaml database.path)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Configuration file used when --database is omitted (default: config.yaml)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("backup"),
        help="Backup directory (default: ./backup)",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=10,
        help="Number of newest snapshots to retain (default: 10)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        database = args.database or database_path_from_config(args.config)
        backup_path = backup_database(database, args.output_dir, args.keep)
    except (OSError, sqlite3.Error, yaml.YAMLError, ValueError, RuntimeError) as error:
        print(f"Backup failed: {error}")
        return 1
    print(f"Backup created: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
