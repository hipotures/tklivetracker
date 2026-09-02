#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from utils.username import normalize_tiktok_username


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "ttracker" / "fav.json"


@dataclass
class TtFavConfig:
    db_path: Path
    recordings_path: Path
    recordings_fav_path: Path
    favorite_source_path: Optional[Path] = None
    project_root: Optional[Path] = None


@dataclass
class TtFavAction:
    username: str
    is_favorite: bool
    label: str


def _absolute_lexical(path: os.PathLike | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _load_config(path: Path) -> TtFavConfig:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    project_root = data.get("project_root")
    recordings_path = Path(data["recordings_path"]).expanduser()
    return TtFavConfig(
        db_path=Path(data["db_path"]).expanduser(),
        recordings_path=recordings_path,
        recordings_fav_path=Path(data["recordings_fav_path"]).expanduser(),
        favorite_source_path=Path(data.get("favorite_source_path", recordings_path)).expanduser(),
        project_root=Path(project_root).expanduser() if project_root else None,
    )


def _relative_first_component(cwd: Path, root: Path) -> Optional[str]:
    try:
        relative = cwd.relative_to(root)
    except ValueError:
        return None

    if not relative.parts:
        return None

    return relative.parts[0]


def determine_action(config: TtFavConfig, cwd: os.PathLike | str) -> Optional[TtFavAction]:
    cwd_path = _absolute_lexical(cwd)
    recordings_root = _absolute_lexical(config.recordings_path)
    fav_root = _absolute_lexical(config.recordings_fav_path)
    source_root = _absolute_lexical(
        config.favorite_source_path or config.recordings_path
    )

    fav_username = _relative_first_component(cwd_path, fav_root)
    if fav_username and normalize_tiktok_username(fav_username, strip_at=False) == fav_username:
        return TtFavAction(fav_username, False, "disabled")

    source_username = _relative_first_component(cwd_path, source_root)
    if source_username and normalize_tiktok_username(source_username, strip_at=False) == source_username:
        return TtFavAction(source_username, True, "enabled")

    recordings_username = _relative_first_component(cwd_path, recordings_root)
    if recordings_username and normalize_tiktok_username(recordings_username, strip_at=False) == recordings_username:
        return TtFavAction(recordings_username, True, "enabled")

    return None


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path.expanduser()), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _current_status(conn: sqlite3.Connection, username: str) -> Optional[int]:
    row = conn.execute("SELECT is_favorite FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        return None
    return int(row["is_favorite"] or 0)


def _update_status(conn: sqlite3.Connection, username: str, is_favorite: bool) -> bool:
    conn.execute(
        "UPDATE users SET is_favorite = ? WHERE username = ?",
        (1 if is_favorite else 0, username),
    )
    conn.commit()
    return True


def _sync_links(config: TtFavConfig, conn: sqlite3.Connection) -> str:
    if config.project_root:
        project_root = str(config.project_root)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

    from modules.favorite_links import format_sync_report, sync_favorite_links

    source_path = config.favorite_source_path or config.recordings_path
    report = sync_favorite_links(conn, source_path, config.recordings_fav_path)
    return format_sync_report(report)


def _print_outside_message(config: TtFavConfig) -> None:
    print("outside configured recording folders")
    print(f"RECORDINGS_PATH: {config.recordings_path}")
    print(f"RECORDINGS_FAV_PATH: {config.recordings_fav_path}")
    print(f"FAVORITE_SOURCE_PATH: {config.favorite_source_path or config.recordings_path}")


def run_ttfav(config: TtFavConfig, cwd: os.PathLike | str, dry_run: bool = False) -> int:
    action = determine_action(config, cwd)
    if action is None:
        _print_outside_message(config)
        conn = _connect(config.db_path)
        try:
            print("syncing favorite links from database")
            print(_sync_links(config, conn))
            return 0
        finally:
            conn.close()

    conn = _connect(config.db_path)
    try:
        current = _current_status(conn, action.username)
        if current is None:
            print(f"user not found in database: {action.username}")
            return 1

        if dry_run:
            verb = "enable" if action.is_favorite else "disable"
            print(f"would {verb} favorite: {action.username}")
            return 0

        _update_status(conn, action.username, action.is_favorite)

        if current == int(action.is_favorite):
            print(f"favorite already {action.label}: {action.username}")
        else:
            print(f"favorite {action.label}: {action.username}")

        print(_sync_links(config, conn))
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Toggle TkLiveTracker favorite status from recording folders.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to installed ttfav config JSON")
    parser.add_argument("--dry-run", action="store_true", help="Print the database action without changing it")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config_path = Path(args.config).expanduser()

    if not config_path.exists():
        print(f"ttfav config not found: {config_path}")
        print("Run the TkLiveTracker tools installer first.")
        return 2

    config = _load_config(config_path)
    cwd = Path(os.environ.get("PWD", os.getcwd()))
    return run_ttfav(config, cwd, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
