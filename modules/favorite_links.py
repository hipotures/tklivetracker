import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from utils.username import normalize_tiktok_username


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    missing_sources: list[str] = field(default_factory=list)

    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.fixed or self.conflicts or self.missing_sources)


def _path(path: os.PathLike | str) -> Path:
    return Path(path).expanduser()


def _same_target(link_path: Path, target_path: Path) -> bool:
    try:
        relative_target = Path(
            os.path.relpath(
                target_path.resolve(strict=False),
                start=link_path.parent.resolve(strict=False),
            )
        )
        return link_path.readlink() == relative_target
    except OSError:
        return False


def _create_relative_symlink(link_path: Path, target_path: Path) -> None:
    relative_target = os.path.relpath(
        target_path.resolve(strict=False),
        start=link_path.parent.resolve(strict=False),
    )
    link_path.symlink_to(relative_target, target_is_directory=True)


def _users_has_column(conn: sqlite3.Connection, column_name: str) -> bool:
    rows = conn.execute("PRAGMA table_info(users)").fetchall()
    return any(row["name"] == column_name for row in rows)


def _get_user_rows(conn: sqlite3.Connection, username: Optional[str] = None) -> list[sqlite3.Row]:
    active_expr = "is_active" if _users_has_column(conn, "is_active") else "1 AS is_active"

    if username is None:
        cursor = conn.execute(f"SELECT username, is_favorite, {active_expr} FROM users ORDER BY username")
        return list(cursor.fetchall())

    cursor = conn.execute(
        f"SELECT username, is_favorite, {active_expr} FROM users WHERE username = ? ORDER BY username",
        (username,),
    )
    return list(cursor.fetchall())


def _unlink_symlink(link_path: Path) -> None:
    if link_path.is_symlink():
        link_path.unlink()


def sync_favorite_links(
    conn: sqlite3.Connection,
    recordings_path: os.PathLike | str,
    recordings_fav_path: os.PathLike | str,
    username: Optional[str] = None,
) -> SyncReport:
    """Synchronize favorite symlinks without creating links for inactive users."""
    recordings_root = _path(recordings_path)
    fav_root = _path(recordings_fav_path)
    fav_root.mkdir(parents=True, exist_ok=True)

    report = SyncReport()
    rows = _get_user_rows(conn, username)
    valid_rows = []
    for row in rows:
        user = row["username"]
        if normalize_tiktok_username(user, strip_at=False) != user:
            report.conflicts.append(f"{user}: invalid TikTok username")
            continue
        valid_rows.append(row)
    rows = valid_rows
    active_usernames = {row["username"] for row in rows if int(row["is_active"] or 0) == 1}
    favorite_usernames = {row["username"] for row in rows if int(row["is_favorite"] or 0) == 1}
    known_usernames = {row["username"] for row in rows}

    for row in rows:
        user = row["username"]
        is_favorite = int(row["is_favorite"] or 0) == 1
        source_path = recordings_root / user
        link_path = fav_root / user

        if is_favorite:
            if user not in active_usernames:
                continue

            if not source_path.is_dir():
                report.missing_sources.append(user)

            if link_path.is_symlink():
                if _same_target(link_path, source_path):
                    continue
                link_path.unlink()
                _create_relative_symlink(link_path, source_path)
                report.fixed.append(user)
                continue

            if link_path.exists():
                report.conflicts.append(f"{user}: {link_path} exists and is not a symlink")
                continue

            _create_relative_symlink(link_path, source_path)
            report.added.append(user)
        else:
            if link_path.is_symlink():
                _unlink_symlink(link_path)
                report.removed.append(user)
            elif link_path.exists():
                report.conflicts.append(f"{user}: {link_path} exists and is not a symlink")

    if username is None and fav_root.exists():
        for entry in fav_root.iterdir():
            if entry.name in favorite_usernames:
                continue
            if entry.name in known_usernames or entry.is_symlink():
                if entry.is_symlink():
                    entry.unlink()
                    if entry.name not in report.removed:
                        report.removed.append(entry.name)
                elif entry.exists():
                    report.conflicts.append(f"{entry.name}: {entry} exists and is not a symlink")

    report.added.sort()
    report.removed.sort()
    report.fixed.sort()
    report.conflicts.sort()
    report.missing_sources.sort()
    return report


def format_sync_report(report: SyncReport) -> str:
    lines: list[str] = []

    if report.added:
        lines.append(f"links added: {', '.join(report.added)}")
    if report.removed:
        lines.append(f"links removed: {', '.join(report.removed)}")
    if report.fixed:
        lines.append(f"links fixed: {', '.join(report.fixed)}")
    if report.missing_sources:
        lines.append(f"missing recording dirs: {', '.join(report.missing_sources)}")
    if report.conflicts:
        lines.append(f"conflicts: {'; '.join(report.conflicts)}")

    if not lines:
        return "favorite links already in sync"

    return "\n".join(lines)
