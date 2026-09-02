import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from utils.config_paths import resolve_path


SCREENSHOT_DATE_DIR_FORMAT = "%Y-%m-%d"
SCREENSHOT_FILENAME_FORMAT = "screenshot-{timestamp}.png"
SCREENSHOT_DATE_DIR_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def resolve_screenshots_dir(raw_dir: Any, base_dir: Optional[str] = None) -> Optional[Path]:
    """Resolve configured screenshots_dir to an absolute path.

    Returns None for null/empty/invalid values instead of guessing a fallback path.
    """
    if raw_dir is None:
        return None

    if not isinstance(raw_dir, str):
        return None

    stripped_dir = raw_dir.strip()
    if not stripped_dir:
        return None

    resolved = resolve_path(stripped_dir, base_dir=base_dir)
    if not resolved or not str(resolved).strip():
        return None

    return Path(resolved).resolve()


def build_screenshot_path(base_dir: Path, current_time: datetime) -> Path:
    """Build screenshot path inside a date-based subdirectory."""
    day_dir = base_dir / current_time.strftime(SCREENSHOT_DATE_DIR_FORMAT)
    timestamp = current_time.replace(microsecond=0).strftime("%Y-%m-%d_%H-%M-%S")
    filename = SCREENSHOT_FILENAME_FORMAT.format(timestamp=timestamp)
    return day_dir / filename


def build_cleanup_candidate_dir(
    base_dir: Path,
    current_time: datetime,
    retention_days: int
) -> Optional[Path]:
    """Build the one date directory that is now older than retention_days."""
    if retention_days <= 0:
        return None

    expired_date = current_time.date() - timedelta(days=retention_days + 1)
    return base_dir / expired_date.strftime(SCREENSHOT_DATE_DIR_FORMAT)


def _is_safe_cleanup_base(base_dir: Path, project_dir: Optional[Path] = None) -> bool:
    """Reject obviously dangerous cleanup roots."""
    try:
        resolved_base = base_dir.resolve()
    except Exception:
        return False

    if not resolved_base.is_absolute():
        return False

    if not resolved_base.exists() or not resolved_base.is_dir() or resolved_base.is_symlink():
        return False

    unsafe_bases = {
        Path(resolved_base.anchor),
        Path.home().resolve(),
        Path.cwd().resolve(),
    }

    if project_dir is not None:
        try:
            unsafe_bases.add(project_dir.resolve())
        except Exception:
            return False

    return resolved_base not in unsafe_bases


def delete_expired_screenshot_dir(
    base_dir: Path,
    candidate_dir: Optional[Path],
    project_dir: Optional[Path] = None
) -> bool:
    """Delete one expired screenshot date directory if all safety checks pass."""
    if candidate_dir is None:
        return False

    if not _is_safe_cleanup_base(base_dir, project_dir=project_dir):
        return False

    if candidate_dir.parent != base_dir:
        return False

    if not SCREENSHOT_DATE_DIR_PATTERN.fullmatch(candidate_dir.name):
        return False

    try:
        datetime.strptime(candidate_dir.name, SCREENSHOT_DATE_DIR_FORMAT)
    except ValueError:
        return False

    if not candidate_dir.exists():
        return False

    if candidate_dir.is_symlink() or not candidate_dir.is_dir():
        return False

    shutil.rmtree(candidate_dir)
    return True
