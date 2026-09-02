import os
from pathlib import Path

from scripts.sync_fav_mtime import sync_link_mtimes


def test_sync_link_mtimes_copies_compressed_directory_mtime_to_symlink(tmp_path: Path) -> None:
    target = tmp_path / "compressed" / "alice"
    link_root = tmp_path / "recordings_fav"
    link = link_root / "alice"
    target.mkdir(parents=True)
    link_root.mkdir()
    link.symlink_to(target, target_is_directory=True)

    target_time = 1_700_000_000
    old_link_time = 1_600_000_000
    os.utime(target, (target_time, target_time))
    os.utime(link, (old_link_time, old_link_time), follow_symlinks=False)

    report = sync_link_mtimes(link_root)

    assert report.updated == ["alice"]
    assert int(link.lstat().st_mtime) == target_time
    assert int(target.stat().st_mtime) == target_time


def test_sync_link_mtimes_dry_run_does_not_touch_symlink(tmp_path: Path) -> None:
    target = tmp_path / "recordings" / "alice"
    link_root = tmp_path / "recordings_fav"
    link = link_root / "alice"
    target.mkdir(parents=True)
    link_root.mkdir()
    link.symlink_to(target, target_is_directory=True)

    target_time = 1_700_000_000
    old_link_time = 1_600_000_000
    os.utime(target, (target_time, target_time))
    os.utime(link, (old_link_time, old_link_time), follow_symlinks=False)

    report = sync_link_mtimes(link_root, dry_run=True)

    assert report.updated == ["alice"]
    assert int(link.lstat().st_mtime) == old_link_time
