import json
import os
import yaml

from utils.config_paths import absolute_config_path, normalize_config_paths

from .enums import Info # Relative import


def banner() -> None:
    """
    Prints a banner with the name of the tool and its version number.
    """
    print(Info.BANNER)


def read_cookies(cookie_file_path=None):
    """
    Loads the config file and returns it. Returns None if no cookies file exists.

    Args:
        cookie_file_path: Optional path to cookie file (from --cookie-file argument)
    """
    # If explicit cookie file path is provided, use it
    if cookie_file_path:
        # Handle relative paths from project root
        if not os.path.isabs(cookie_file_path):
            script_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.join(script_dir, "..", "..")
            cookie_file_path = os.path.join(project_root, cookie_file_path)
            cookie_file_path = os.path.normpath(cookie_file_path)

        if os.path.exists(cookie_file_path):
            with open(cookie_file_path, "r") as f:
                return json.load(f)
        else:
            print(f"WARNING: Cookie file not found: {cookie_file_path}")
            return None

    # Otherwise, try default locations
    # Try new location first (private/cookies.json), fallback to old location
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.join(script_dir, "..", "..")

    # Try private/cookies_full.json first (new unified format)
    new_config_path = os.path.join(project_root, "private", "cookies_full.json")
    if os.path.exists(new_config_path):
        with open(new_config_path, "r") as f:
            return json.load(f)

    # Try private/cookies.json (legacy)
    legacy_config_path = os.path.join(project_root, "private", "cookies.json")
    if os.path.exists(legacy_config_path):
        with open(legacy_config_path, "r") as f:
            return json.load(f)

    # Fallback to old location
    old_config_path = os.path.join(script_dir, "..", "cookies.json")
    if os.path.exists(old_config_path):
        with open(old_config_path, "r") as f:
            return json.load(f)

    # No cookies file found
    return None


def read_telegram_config(config_path):
    """Load recorder upload credentials from the active config file."""
    config_path = absolute_config_path(config_path)
    with open(config_path, "r", encoding="utf-8") as config_file:
        config = normalize_config_paths(yaml.safe_load(config_file) or {}, config_path)
    return config.get("telegram", {}).get("upload", {})
