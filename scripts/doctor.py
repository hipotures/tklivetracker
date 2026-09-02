#!/usr/bin/env python3
"""Offline installation and configuration checks for TkLiveTracker."""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.config_paths import absolute_config_path, normalize_config_paths


REQUIRED_IMPORTS = ("curl_cffi", "flask", "psutil", "requests", "selenium", "yaml")
CHROME_COMMANDS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


class Doctor:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, message: str) -> None:
        print(f"✓ {message}")

    def warn(self, message: str) -> None:
        self.warnings += 1
        print(f"! {message}")

    def fail(self, message: str) -> None:
        self.failures += 1
        print(f"✗ {message}")


def _find_chrome(config: dict) -> str | None:
    configured = config.get("selenium", {}).get("chrome_binary_path")
    if configured:
        path = Path(configured)
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return next((path for name in CHROME_COMMANDS if (path := shutil.which(name))), None)


def _check_private_mode(doctor: Doctor, path: Path, expected: int, label: str) -> None:
    if os.name != "posix" or not path.exists():
        return
    actual = stat.S_IMODE(path.stat().st_mode)
    if actual & ~expected:
        doctor.fail(f"{label} permissions are {actual:04o}; expected {expected:04o}")
    else:
        doctor.ok(f"{label} permissions ({actual:04o})")


def _writable_directory(doctor: Doctor, path: Path, label: str, mode: int = 0o755) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        if os.name == "posix" and mode == 0o700:
            path.chmod(0o700)
        fd, probe = tempfile.mkstemp(prefix=".doctor-", dir=path)
        os.close(fd)
        os.unlink(probe)
    except OSError as error:
        doctor.fail(f"{label} is not writable ({error.strerror or error})")
    else:
        doctor.ok(f"{label} writable")


def run(config_path: str) -> int:
    doctor = Doctor()
    print("TkLiveTracker doctor\n")

    if sys.version_info < (3, 12):
        doctor.fail(f"Python {sys.version_info.major}.{sys.version_info.minor}; 3.12+ required")
    else:
        doctor.ok(f"Python {sys.version_info.major}.{sys.version_info.minor}")

    path = Path(absolute_config_path(config_path))
    if not path.is_file():
        doctor.fail(f"Configuration not found: {path}")
        return 1
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("top level must be a mapping")
        config = normalize_config_paths(loaded, str(path))
    except (OSError, yaml.YAMLError, ValueError) as error:
        doctor.fail(f"Configuration is invalid: {error}")
        return 1
    doctor.ok("Configuration loaded")
    _check_private_mode(doctor, path.resolve(), 0o600, "config.yaml")

    chrome = _find_chrome(config)
    if chrome:
        doctor.ok(f"Chrome/Chromium found ({chrome})")
    else:
        configured_chrome = config.get("selenium", {}).get("chrome_binary_path")
        if configured_chrome:
            doctor.fail(
                "Configured selenium.chrome_binary_path is not executable: "
                f"{configured_chrome}"
            )
        else:
            doctor.fail("Chrome/Chromium executable not found in PATH")

    missing_imports = []
    for module_name in REQUIRED_IMPORTS:
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing_imports.append(module_name)
    if missing_imports:
        doctor.fail("Missing runtime dependencies: " + ", ".join(missing_imports))
    else:
        doctor.ok("Runtime dependencies import")

    paths = config.get("paths", {})
    database_path = config.get("database", {}).get("path")
    recordings_path = paths.get("recordings_path")
    log_path = paths.get("log_path")
    required = {
        "paths.recordings_path": recordings_path,
        "paths.log_path": log_path,
        "database.path": database_path,
    }
    missing = [name for name, value in required.items() if not isinstance(value, str) or not value]
    if missing:
        doctor.fail("Missing required configuration: " + ", ".join(missing))
    else:
        _writable_directory(doctor, Path(recordings_path), "Recordings directory")
        _writable_directory(doctor, Path(log_path), "Log directory")
        _writable_directory(doctor, Path(database_path).parent, "Database directory")

    private_dir = path.parent / "private"
    _writable_directory(doctor, private_dir, "Private runtime directory", mode=0o700)
    _check_private_mode(doctor, private_dir, 0o700, "Private runtime directory")

    web = config.get("web_monitor", {})
    host = web.get("host", "0.0.0.0")
    port = web.get("port", 5001)
    if not isinstance(host, str) or not host.strip() or any(char.isspace() for char in host):
        doctor.fail("web_monitor.host is invalid")
    else:
        doctor.ok("Web host is valid")
    if isinstance(port, bool):
        port_valid = False
    else:
        try:
            port_valid = 1 <= int(port) <= 65535
        except (TypeError, ValueError):
            port_valid = False
    if port_valid:
        doctor.ok(f"Web port is valid ({int(port)})")
    else:
        doctor.fail("web_monitor.port must be between 1 and 65535")

    cookies_path = config.get("cookies", {}).get("cookie_json_file")
    profile_path = config.get("selenium", {}).get("chrome_profile_path")
    session_initialized = bool(cookies_path and Path(cookies_path).is_file()) or bool(
        profile_path and Path(profile_path).is_dir() and any(Path(profile_path).iterdir())
    )
    if session_initialized:
        doctor.ok("TikTok browser session appears initialized")
    else:
        doctor.warn("TikTok browser session has not been initialized")

    print()
    if doctor.failures:
        print(f"Doctor found {doctor.failures} blocking problem(s).")
        return 1
    print("Installation is usable.")
    if not session_initialized:
        print("Next:")
        print(f"uv run python scripts/selenium_live_monitor.py --config {path} --headless-off")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    args = parser.parse_args(argv)
    return run(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
