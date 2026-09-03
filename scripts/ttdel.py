#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "ttracker" / "fav.json"


@dataclass
class TtDelConfig:
    db_path: Path
    recordings_path: Path
    recordings_fav_path: Path
    inactive_users_path: Path
    favorite_source_path: Path


def _absolute_lexical(path: os.PathLike | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _load_config(path: Path) -> TtDelConfig:
    with path.open("r", encoding="utf-8") as config_file:
        data = json.load(config_file)

    return TtDelConfig(
        db_path=Path(data["db_path"]).expanduser(),
        recordings_path=Path(data["recordings_path"]).expanduser(),
        recordings_fav_path=Path(data["recordings_fav_path"]).expanduser(),
        inactive_users_path=Path(data["inactive_users_path"]).expanduser(),
        favorite_source_path=Path(data["favorite_source_path"]).expanduser(),
    )


def _relative_first_component(cwd: Path, root: Path) -> Optional[str]:
    try:
        relative = cwd.relative_to(root)
    except ValueError:
        return None

    if not relative.parts:
        return None

    return relative.parts[0]


def determine_username(config: TtDelConfig, cwd: os.PathLike | str) -> Optional[str]:
    cwd_path = _absolute_lexical(cwd)
    roots = (
        config.recordings_fav_path,
        config.recordings_path,
        config.favorite_source_path,
    )

    for root in roots:
        username = _relative_first_component(cwd_path, _absolute_lexical(root))
        if username:
            return username

    return None


def run_ttdel(config: TtDelConfig, cwd: os.PathLike | str, dry_run: bool = False) -> int:
    username = determine_username(config, cwd)
    if username is None:
        print("outside configured user folders")
        print(f"RECORDINGS_PATH: {config.recordings_path}")
        print(f"RECORDINGS_FAV_PATH: {config.recordings_fav_path}")
        print(f"FAVORITE_SOURCE_PATH: {config.favorite_source_path}")
        return 1

    source_path = config.recordings_path / username
    destination_path = config.inactive_users_path / username
    if source_path.parent != config.recordings_path or destination_path.parent != config.inactive_users_path:
        print(f"refusing unsafe user path: {username}")
        return 1
    if source_path.is_symlink():
        print(f"refusing symlinked recording directory: {source_path}")
        return 1
    source_exists = source_path.exists() or source_path.is_symlink()
    destination_exists = destination_path.exists() or destination_path.is_symlink()
    if source_exists and destination_exists:
        moved_at = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination_path = config.inactive_users_path / f"{username}_{moved_at}"

    conn = sqlite3.connect(str(config.db_path.expanduser()), timeout=30.0)
    try:
        row = conn.execute(
            "SELECT is_active FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            print(f"user not found in database: {username}")
            return 1
        if int(row[0]) == -1:
            print(f"user is marked as deleted: {username}")
            return 1
        process_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'live_processes'"
        ).fetchone()
        if process_table is not None:
            active_process = conn.execute(
                "SELECT 1 FROM live_processes WHERE username = ? AND is_active = 1 LIMIT 1",
                (username,),
            ).fetchone()
            if active_process is not None:
                print(f"refusing to deactivate {username}: active recorder is registered")
                return 1
        if destination_path.exists() or destination_path.is_symlink():
            print(f"destination already exists: {destination_path}")
            return 1

        if dry_run:
            print(f"would deactivate user: {username}")
            if source_exists:
                print(f"would move: {source_path} -> {destination_path}")
            else:
                print(f"recording directory not found: {source_path}")
            return 0

        if int(row[0]) != 0:
            conn.execute(
                """
                UPDATE users
                SET is_active = 0,
                    last_deactivated_at = DATETIME('now', 'localtime')
                WHERE username = ?
                """,
                (username,),
            )

        if source_exists:
            config.inactive_users_path.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_path), str(destination_path))
            print(f"moved: {source_path} -> {destination_path}")
        else:
            print(f"recording directory not found: {source_path}")

        conn.commit()
        if int(row[0]) == 0:
            print(f"user already inactive: {username}")
        else:
            print(f"deactivated user: {username}")
        return 0
    except (OSError, sqlite3.Error) as exc:
        conn.rollback()
        print(f"failed to deactivate {username}: {exc}")
        return 1
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deactivate the TkLiveTracker user for the current recording directory."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to tools config JSON")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without changing anything")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = Path(args.config).expanduser()

    if not config_path.exists():
        print(f"tools config not found: {config_path}")
        print("Run the TkLiveTracker tools installer first.")
        return 2

    config = _load_config(config_path)
    cwd = Path(os.environ.get("PWD", os.getcwd()))
    return run_ttdel(config, cwd, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
