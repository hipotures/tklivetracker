#!/usr/bin/env python3
import json
import argparse
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.config_paths import (
    absolute_config_path,
    resolve_config_placeholders,
    resolve_path,
)


def _load_project_config(project_root: Path, config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as f:
        return resolve_config_placeholders(yaml.safe_load(f) or {})


def _normalize_api_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("API URL must be HTTP(S) without credentials, query, or fragment")
    return normalized


def _default_api_url(config: dict) -> str:
    web_config = config.get("web_monitor", {})
    configured_url = web_config.get("api_url")
    if configured_url:
        return _normalize_api_url(str(configured_url))

    host = str(web_config.get("host", "0.0.0.0")).strip()
    port = int(web_config.get("port", 5001))
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return _normalize_api_url(f"http://{host}:{port}")


def _build_tools_config(
    project_root: Path,
    config: dict,
    config_dir: Path,
    api_url: str | None = None,
) -> dict:
    paths = config.get("paths", {})
    recordings_path = resolve_path(paths.get("recordings_path"), base_dir=str(config_dir))
    recordings_fav_path = resolve_path(paths.get("recordings_fav_path"), base_dir=str(config_dir))
    inactive_users_path = resolve_path(paths.get("inactive_users_path"), base_dir=str(config_dir))
    favorite_source_path = resolve_path(
        config.get("persistent_live_system", {}).get("compressed_output_path"),
        base_dir=str(config_dir),
    )
    db_path = resolve_path(config.get("database", {}).get("path"), base_dir=str(config_dir))

    missing = []
    if not recordings_path:
        missing.append("paths.recordings_path")
    if not recordings_fav_path:
        missing.append("paths.recordings_fav_path")
    if not inactive_users_path:
        missing.append("paths.inactive_users_path")
    if not favorite_source_path:
        missing.append("persistent_live_system.compressed_output_path")
    if not db_path:
        missing.append("database.path")

    if missing:
        raise ValueError(f"Missing required config value(s): {', '.join(missing)}")

    return {
        "project_root": str(project_root),
        "db_path": db_path,
        "api_url": _normalize_api_url(api_url) if api_url else _default_api_url(config),
        "recordings_path": recordings_path,
        "recordings_fav_path": recordings_fav_path,
        "inactive_users_path": inactive_users_path,
        "favorite_source_path": favorite_source_path,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install TkLiveTracker helper tools")
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config.yaml",
        help="Path to config.yaml for a full installation (default: project config.yaml)",
    )
    parser.add_argument(
        "--api-url",
        help=(
            "Web monitor URL used by ttfav and ttdel (for example "
            "http://tracker.example:5001); overrides web_monitor.api_url"
        ),
    )
    parser.add_argument(
        "--api-tools-only",
        action="store_true",
        help="Install only API-based ttfav and ttdel (recommended on a remote client)",
    )
    args = parser.parse_args(argv)
    project_root = PROJECT_ROOT
    if args.api_tools_only:
        if not args.api_url:
            parser.error("--api-tools-only requires --api-url")
        config = {"api_url": _normalize_api_url(args.api_url)}
    else:
        config_path = Path(absolute_config_path(args.config))
        config = _build_tools_config(
            project_root,
            _load_project_config(project_root, config_path),
            config_path.parent,
            api_url=args.api_url,
        )

    bin_dir = Path.home() / ".local" / "bin"
    config_dir = Path.home() / ".config" / "ttracker"
    bin_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    source_ttfav_script = project_root / "scripts" / "ttfav.py"
    source_ttdel_script = project_root / "scripts" / "ttdel.py"
    source_mtime_script = project_root / "scripts" / "sync_fav_mtime.py"
    destination_ttfav_script = bin_dir / "ttfav"
    destination_ttdel_script = bin_dir / "ttdel"
    legacy_fav_script = bin_dir / "fav"
    destination_mtime_script = bin_dir / "fav-mtime"
    destination_config = config_dir / "fav.json"

    shutil.copy2(source_ttfav_script, destination_ttfav_script)
    destination_ttfav_script.chmod(0o755)
    shutil.copy2(source_ttdel_script, destination_ttdel_script)
    destination_ttdel_script.chmod(0o755)
    removed_legacy_fav = False
    if not args.api_tools_only:
        recordings_fav_dir = Path(config["recordings_fav_path"])
        local_mtime_script = recordings_fav_dir / "fav-mtime"
        recordings_fav_dir.mkdir(parents=True, exist_ok=True)
        removed_legacy_fav = legacy_fav_script.exists() or legacy_fav_script.is_symlink()
        if removed_legacy_fav:
            legacy_fav_script.unlink()
        shutil.copy2(source_mtime_script, destination_mtime_script)
        destination_mtime_script.chmod(0o755)
        shutil.copy2(source_mtime_script, local_mtime_script)
        local_mtime_script.chmod(0o755)

    with destination_config.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    print("TkLiveTracker tools installed")
    print(f"ttfav command: {destination_ttfav_script}")
    print(f"ttdel command: {destination_ttdel_script}")
    if not args.api_tools_only:
        if removed_legacy_fav:
            print(f"removed legacy command: {legacy_fav_script}")
        print(f"mtime command: {destination_mtime_script}")
        print(f"mtime click file: {local_mtime_script}")
    print(f"config: {destination_config}")
    print(f"API_URL: {config['api_url']}")
    if not args.api_tools_only:
        print(f"RECORDINGS_PATH: {config['recordings_path']}")
        print(f"RECORDINGS_FAV_PATH: {config['recordings_fav_path']}")
        print(f"INACTIVE_USERS_PATH: {config['inactive_users_path']}")
        print(f"FAVORITE_SOURCE_PATH: {config['favorite_source_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
