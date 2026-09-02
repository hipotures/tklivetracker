import os
from pathlib import Path
from typing import Any, Optional


COOKIE_PATH_KEYS = (
    "cookie_json_file",
    "firefox_cookie_db_path",
    "target_cookie_json_path",
)

CONFIG_PATH_KEYS = {
    "paths": (
        "recordings_path",
        "recordings_fav_path",
        "inactive_users_path",
        "log_path",
    ),
    "database": ("path",),
    "persistent_live_system": (
        "metadata_path",
        "compressed_output_path",
        "lock_file_path",
        "recorder_log_path",
    ),
    "selenium": (
        "database_path",
        "db_path",
        "chrome_binary_path",
        "chrome_profile_path",
        "cache_dir",
        "screenshots_dir",
    ),
}


def absolute_config_path(path_value: Any) -> str:
    """Return an absolute config path without dereferencing a config symlink."""
    return os.path.abspath(os.path.expanduser(os.fspath(path_value)))


def resolve_config_placeholders(obj: Any) -> Any:
    """Expand user-home markers across a config structure."""
    if isinstance(obj, dict):
        return {key: resolve_config_placeholders(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [resolve_config_placeholders(item) for item in obj]
    if isinstance(obj, str):
        return os.path.expanduser(obj)
    return obj


def resolve_path(path_value: Optional[str], base_dir: Optional[str] = None) -> Optional[str]:
    """Expand ~ and anchor relative paths to base_dir when provided."""
    if path_value is None:
        return None

    resolved = os.path.expanduser(path_value)
    if not resolved:
        return resolved

    if os.path.isabs(resolved):
        return resolved

    if base_dir:
        return str(Path(base_dir, resolved).resolve())

    return os.path.abspath(resolved)


def resolve_cookie_paths(cookies_config: Any, base_dir: str) -> dict:
    """Return cookie configuration with filesystem paths anchored to the config directory."""
    resolved = dict(cookies_config or {})
    for key in COOKIE_PATH_KEYS:
        value = resolved.get(key)
        if isinstance(value, str):
            resolved[key] = resolve_path(value, base_dir=base_dir)
    return resolved


def normalize_config_paths(config: Any, config_path: str) -> dict:
    """Resolve configured filesystem paths relative to the active config file."""
    normalized = resolve_config_placeholders(config or {})
    config_dir = str(Path(absolute_config_path(config_path)).parent)

    for section_name, keys in CONFIG_PATH_KEYS.items():
        section = normalized.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in keys:
            value = section.get(key)
            if isinstance(value, str):
                section[key] = resolve_path(value, base_dir=config_dir)

    normalized["cookies"] = resolve_cookie_paths(
        normalized.get("cookies"), config_dir
    )

    notifications = normalized.get("telegram", {}).get("notifications")
    if isinstance(notifications, dict) and isinstance(notifications.get("state_file"), str):
        notifications["state_file"] = resolve_path(
            notifications["state_file"], base_dir=config_dir
        )
    upload = normalized.get("telegram", {}).get("upload")
    if isinstance(upload, dict) and isinstance(upload.get("session_path"), str):
        upload["session_path"] = resolve_path(upload["session_path"], base_dir=config_dir)

    return normalized
