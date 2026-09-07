import sqlite3
from pathlib import Path

from modules.favorite_links import SyncReport, format_sync_report, sync_favorite_links


def _create_users_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            is_favorite INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    return conn


def test_sync_favorite_links_adds_and_removes_only_symlinks(tmp_path: Path) -> None:
    conn = _create_users_db(tmp_path / "users.db")
    conn.executemany(
        "INSERT INTO users (username, is_favorite) VALUES (?, ?)",
        [("fav_user", 1), ("plain_user", 0)],
    )
    conn.commit()

    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "fav_user").mkdir(parents=True)
    (recordings_path / "plain_user").mkdir(parents=True)
    fav_path.mkdir()
    (fav_path / "plain_user").symlink_to(recordings_path / "plain_user", target_is_directory=True)

    report = sync_favorite_links(conn, recordings_path, fav_path)

    assert (fav_path / "fav_user").is_symlink()
    assert (fav_path / "fav_user").readlink() == Path("../recordings/fav_user")
    assert (fav_path / "fav_user").resolve() == (recordings_path / "fav_user").resolve()
    assert not (fav_path / "plain_user").exists()
    assert (recordings_path / "plain_user").is_dir()
    assert report.added == ["fav_user"]
    assert report.removed == ["plain_user"]
    assert report.conflicts == []


def test_sync_favorite_links_reports_conflict_for_real_directory(tmp_path: Path) -> None:
    conn = _create_users_db(tmp_path / "users.db")
    conn.execute("INSERT INTO users (username, is_favorite) VALUES (?, ?)", ("fav_user", 1))
    conn.commit()

    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "fav_user").mkdir(parents=True)
    (fav_path / "fav_user").mkdir(parents=True)

    report = sync_favorite_links(conn, recordings_path, fav_path)

    assert (fav_path / "fav_user").is_dir()
    assert not (fav_path / "fav_user").is_symlink()
    assert report.added == []
    assert len(report.conflicts) == 1
    assert "not a symlink" in report.conflicts[0]


def test_sync_favorite_links_replaces_absolute_symlink_with_relative_target(tmp_path: Path) -> None:
    conn = _create_users_db(tmp_path / "users.db")
    conn.execute("INSERT INTO users (username, is_favorite) VALUES (?, ?)", ("fav_user", 1))
    conn.commit()

    tt_root = tmp_path / "TT"
    compressed_path = tt_root / "compressed"
    fav_path = tt_root / "recordings_fav"
    (compressed_path / "fav_user").mkdir(parents=True)
    fav_path.mkdir()
    link = fav_path / "fav_user"
    link.symlink_to(compressed_path / "fav_user", target_is_directory=True)

    report = sync_favorite_links(conn, compressed_path, fav_path)

    assert link.readlink() == Path("../compressed/fav_user")
    assert report.fixed == ["fav_user"]

    second_report = sync_favorite_links(conn, compressed_path, fav_path)
    assert not second_report.has_changes()

    archive_root = tmp_path / "archive"
    tt_root.rename(archive_root)
    archived_link = archive_root / "recordings_fav" / "fav_user"
    assert archived_link.resolve() == (archive_root / "compressed" / "fav_user").resolve()


def test_sync_favorite_links_removes_symlink_for_unknown_user(tmp_path: Path) -> None:
    conn = _create_users_db(tmp_path / "users.db")
    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "deleted_user").mkdir(parents=True)
    fav_path.mkdir()
    (fav_path / "deleted_user").symlink_to(recordings_path / "deleted_user", target_is_directory=True)

    report = sync_favorite_links(conn, recordings_path, fav_path)

    assert not (fav_path / "deleted_user").exists()
    assert report.removed == ["deleted_user"]


def test_sync_favorite_links_ignores_inactive_favorites_but_removes_inactive_nonfavorites(
    tmp_path: Path,
) -> None:
    conn = _create_users_db(tmp_path / "users.db")
    conn.executemany(
        "INSERT INTO users (username, is_active, is_favorite) VALUES (?, ?, ?)",
        [
            ("active_fav", 1, 1),
            ("inactive_fav", 0, 1),
            ("deleted_fav", -1, 1),
            ("inactive_plain", 0, 0),
        ],
    )
    conn.commit()

    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "active_fav").mkdir(parents=True)
    fav_path.mkdir()
    (fav_path / "inactive_fav").symlink_to(
        recordings_path / "inactive_fav",
        target_is_directory=True,
    )
    (fav_path / "inactive_plain").symlink_to(
        recordings_path / "inactive_plain",
        target_is_directory=True,
    )

    report = sync_favorite_links(conn, recordings_path, fav_path)

    assert (fav_path / "active_fav").is_symlink()
    assert (fav_path / "inactive_fav").is_symlink()
    assert (fav_path / "inactive_fav").readlink() == Path("../recordings/inactive_fav")
    assert not (fav_path / "inactive_plain").is_symlink()
    assert not (fav_path / "deleted_fav").exists()
    assert report.added == ["active_fav"]
    assert report.fixed == ["inactive_fav"]
    assert report.removed == ["inactive_plain"]
    assert report.missing_sources == []


def test_format_sync_report_lists_changes() -> None:
    report = SyncReport(
        added=["a"],
        removed=["b"],
        fixed=["c"],
        conflicts=["d conflict"],
        missing_sources=["e"],
    )

    output = format_sync_report(report)

    assert "links added: a" in output
    assert "links removed: b" in output
    assert "links fixed: c" in output
    assert "missing recording dirs: e" in output
    assert "conflicts: d conflict" in output


def test_sync_favorite_links_targets_missing_source_for_future_recordings(tmp_path: Path) -> None:
    conn = _create_users_db(tmp_path / "users.db")
    conn.execute("INSERT INTO users (username, is_favorite) VALUES (?, ?)", ("fav_user", 1))
    conn.commit()

    compressed_path = tmp_path / "compressed"
    recordings_path = tmp_path / "recordings"
    fav_path = tmp_path / "recordings_fav"
    (recordings_path / "fav_user").mkdir(parents=True)
    fav_path.mkdir()
    (fav_path / "fav_user").symlink_to(
        recordings_path / "fav_user",
        target_is_directory=True,
    )

    report = sync_favorite_links(conn, compressed_path, fav_path)

    link = fav_path / "fav_user"
    assert link.is_symlink()
    assert link.readlink() == Path("../compressed/fav_user")
    assert not link.exists()
    assert report.fixed == ["fav_user"]
    assert report.missing_sources == ["fav_user"]


def test_sync_favorite_links_does_not_use_invalid_database_username_as_path(tmp_path: Path) -> None:
    conn = _create_users_db(tmp_path / "users.db")
    conn.execute(
        "INSERT INTO users (username, is_favorite) VALUES (?, ?)",
        ("../outside", 1),
    )
    conn.commit()

    report = sync_favorite_links(
        conn, tmp_path / "recordings", tmp_path / "recordings_fav"
    )

    assert report.added == []
    assert report.conflicts == ["../outside: invalid TikTok username"]
    assert not (tmp_path / "outside").exists()
