from datetime import datetime
from pathlib import Path

from utils.screenshot_retention import (
    build_cleanup_candidate_dir,
    build_screenshot_path,
    delete_expired_screenshot_dir,
    resolve_screenshots_dir,
)


def test_resolve_screenshots_dir_rejects_null_and_empty(tmp_path: Path) -> None:
    assert resolve_screenshots_dir(None, base_dir=str(tmp_path)) is None
    assert resolve_screenshots_dir("", base_dir=str(tmp_path)) is None
    assert resolve_screenshots_dir("   ", base_dir=str(tmp_path)) is None


def test_build_screenshot_path_uses_day_subdirectory(tmp_path: Path) -> None:
    current_time = datetime(2026, 3, 17, 14, 25, 0)

    screenshot_path = build_screenshot_path(tmp_path, current_time)

    assert screenshot_path == (
        tmp_path / "2026-03-17" / "screenshot-2026-03-17_14-25-00.png"
    )


def test_build_cleanup_candidate_dir_targets_one_expired_day(tmp_path: Path) -> None:
    current_time = datetime(2026, 3, 17, 14, 25, 0)

    candidate_dir = build_cleanup_candidate_dir(tmp_path, current_time, retention_days=30)

    assert candidate_dir == tmp_path / "2026-02-14"


def test_delete_expired_screenshot_dir_deletes_only_direct_child_date_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    screenshots_dir = project_dir / "tmp" / "screenshots"
    screenshots_dir.mkdir(parents=True)
    expired_dir = screenshots_dir / "2026-02-14"
    expired_dir.mkdir()
    (expired_dir / "shot.png").write_text("x")

    deleted = delete_expired_screenshot_dir(
        screenshots_dir,
        expired_dir,
        project_dir=project_dir
    )

    assert deleted is True
    assert not expired_dir.exists()


def test_delete_expired_screenshot_dir_skips_invalid_target(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    screenshots_dir = project_dir / "tmp" / "screenshots"
    screenshots_dir.mkdir(parents=True)

    invalid_dir = screenshots_dir / "latest"
    invalid_dir.mkdir()

    deleted = delete_expired_screenshot_dir(
        screenshots_dir,
        invalid_dir,
        project_dir=project_dir
    )

    assert deleted is False
    assert invalid_dir.exists()


def test_delete_expired_screenshot_dir_skips_when_base_is_project_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    candidate_dir = project_dir / "2026-02-14"
    candidate_dir.mkdir()

    deleted = delete_expired_screenshot_dir(
        project_dir,
        candidate_dir,
        project_dir=project_dir
    )

    assert deleted is False
    assert candidate_dir.exists()
