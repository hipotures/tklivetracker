import sqlite3
import time
from pathlib import Path

import pytest

import scripts.ttfav as ttfav
from modules.favorite_links import sync_favorite_links
from scripts.ttfav import TtFavConfig, determine_action, run_ttfav


USER_COUNT = 5_000
TARGET_USERNAME = "target_user"


def _create_sync_scenario(
    root: Path,
    *,
    is_favorite: bool,
) -> tuple[sqlite3.Connection, Path, Path]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE users (
            username TEXT PRIMARY KEY,
            is_favorite INTEGER NOT NULL,
            is_active INTEGER NOT NULL
        )
        """
    )
    rows = [
        (f"user{index:05d}", 0, 1)
        for index in range(USER_COUNT - 1)
    ]
    rows.append((TARGET_USERNAME, int(is_favorite), 1))
    conn.executemany("INSERT INTO users VALUES (?, ?, ?)", rows)
    conn.commit()

    recordings_path = root / "recordings"
    favorites_path = root / "recordings_fav"
    target_recordings_path = recordings_path / TARGET_USERNAME
    target_recordings_path.mkdir(parents=True)
    favorites_path.mkdir()

    if not is_favorite:
        (favorites_path / TARGET_USERNAME).symlink_to(
            target_recordings_path,
            target_is_directory=True,
        )

    return conn, recordings_path, favorites_path


@pytest.mark.parametrize("is_favorite", [False, True], ids=["disable", "enable"])
def test_scoped_favorite_sync_reports_optimization(
    tmp_path: Path,
    monkeypatch,
    is_favorite: bool,
) -> None:
    full = _create_sync_scenario(tmp_path / "full", is_favorite=is_favorite)
    scoped = _create_sync_scenario(tmp_path / "scoped", is_favorite=is_favorite)

    counters = {
        "full": {"filesystem_calls": 0},
        "scoped": {"filesystem_calls": 0},
    }
    active_counter = {"name": "full"}

    def count_call(original):
        def counted(path, *args, **kwargs):
            counters[active_counter["name"]]["filesystem_calls"] += 1
            return original(path, *args, **kwargs)

        return counted

    for method_name in ("exists", "is_dir", "is_symlink", "iterdir", "readlink"):
        monkeypatch.setattr(
            Path,
            method_name,
            count_call(getattr(Path, method_name)),
        )

    full_conn, full_recordings, full_favorites = full
    started = time.perf_counter_ns()
    full_report = sync_favorite_links(
        full_conn,
        full_recordings,
        full_favorites,
    )
    full_ns = time.perf_counter_ns() - started

    active_counter["name"] = "scoped"
    scoped_conn, scoped_recordings, scoped_favorites = scoped
    started = time.perf_counter_ns()
    scoped_report = sync_favorite_links(
        scoped_conn,
        scoped_recordings,
        scoped_favorites,
        username=TARGET_USERNAME,
    )
    scoped_ns = time.perf_counter_ns() - started

    full_calls = counters["full"]["filesystem_calls"]
    scoped_calls = counters["scoped"]["filesystem_calls"]
    target_should_exist = is_favorite
    assert (full_favorites / TARGET_USERNAME).is_symlink() is target_should_exist
    assert (scoped_favorites / TARGET_USERNAME).is_symlink() is target_should_exist
    assert full_report.added == scoped_report.added
    assert full_report.removed == scoped_report.removed
    assert full_report.fixed == scoped_report.fixed
    assert full_report.conflicts == scoped_report.conflicts
    assert full_report.missing_sources == scoped_report.missing_sources

    operation_reduction = 1 - (scoped_calls / full_calls)
    elapsed_reduction = 1 - (scoped_ns / full_ns)

    assert operation_reduction >= 0.99
    print(
        f"sync {('enable' if is_favorite else 'disable')}: "
        f"users={USER_COUNT}, filesystem_calls={full_calls}->{scoped_calls} "
        f"({operation_reduction:.2%} reduction), "
        f"elapsed={full_ns / 1_000_000:.3f}ms->{scoped_ns / 1_000_000:.3f}ms "
        f"({elapsed_reduction:.2%} reduction)"
    )


def test_single_request_explicit_action_reports_optimization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = TtFavConfig(api_url="http://tracker.example:5001")
    cwd = tmp_path / TARGET_USERNAME
    calls = []

    def delayed_api_request(config, method, path, data=None):
        calls.append((method, path, data))
        time.sleep(0.025)
        if method == "GET":
            return {
                "user": {
                    "username": TARGET_USERNAME,
                    "is_favorite": False,
                }
            }
        return {"message": "updated", "is_favorite": True}

    monkeypatch.setattr(ttfav, "_api_request", delayed_api_request)

    started = time.perf_counter_ns()
    assert run_ttfav(config, cwd, requested_action="add") == 0
    current_ns = time.perf_counter_ns() - started
    current_calls = len(calls)

    calls.clear()
    action = determine_action(cwd, requested_action="add")
    assert action is not None
    started = time.perf_counter_ns()
    ttfav._api_request(
        config,
        "PUT",
        f"/api/users/{action.username}/favorite",
        {"is_favorite": action.is_favorite},
    )
    proposed_ns = time.perf_counter_ns() - started
    proposed_calls = len(calls)

    request_reduction = 1 - (proposed_calls / current_calls)
    elapsed_reduction = 1 - (proposed_ns / current_ns)

    assert current_calls == 2
    assert proposed_calls == 1
    assert request_reduction == 0.5
    print(
        f"explicit add: requests={current_calls}->{proposed_calls} "
        f"({request_reduction:.2%} reduction), "
        f"controlled elapsed={current_ns / 1_000_000:.3f}ms"
        f"->{proposed_ns / 1_000_000:.3f}ms "
        f"({elapsed_reduction:.2%} reduction)"
    )
