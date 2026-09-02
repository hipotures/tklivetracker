#!/usr/bin/env python3
"""Create anonymized TkLiveTracker screenshots from a running local dashboard.

The script is intentionally external to the web application. It:
- opens a fresh Chrome/Chromium profile (or attaches to an existing CDP browser),
- captures a fixed desktop viewport through Chrome DevTools Protocol,
- replays screenshot recipes from docs/screenshots/public_screenshots.yaml,
- provides a headed --record mode for creating recipes without editing Python,
- can publish a reviewed /tmp capture set into docs/screenshots and update README,
- can optionally commit only the screenshot/README/recipe changes,
- disables auto-refresh/SSE during capture,
- pseudonymizes usernames consistently for the whole run,
- redacts local filesystem paths shown on the Admin page,
- fails closed if a known username is still present in username-bearing UI nodes.

Recorder mode never writes username mappings or text-input values to recipes.
No username mapping is written to disk.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
import yaml
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait


DEFAULT_BASE_URL = "http://127.0.0.1:5001"
DEFAULT_WIDTH = 1440
DEFAULT_HEIGHT = 1000
DEFAULT_TIMEOUT = 20.0
DEFAULT_SETTLE = 0.65

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent
DEFAULT_PUBLIC_SCREENSHOT_DIR = REPO_ROOT / "docs" / "screenshots"
DEFAULT_RECIPES_PATH = DEFAULT_PUBLIC_SCREENSHOT_DIR / "public_screenshots.yaml"
DEFAULT_README_PATH = REPO_ROOT / "README.md"
LEGACY_RECIPES_PATH = SCRIPT_PATH.with_name("public_screenshots.yaml")

README_SCREENSHOTS_BEGIN = "<!-- BEGIN GENERATED PUBLIC SCREENSHOTS -->"
README_SCREENSHOTS_END = "<!-- END GENERATED PUBLIC SCREENSHOTS -->"

# Seed recipe set. Once docs/screenshots/public_screenshots.yaml exists, the YAML
# file becomes the source of truth and this list is used only to create it initially.
DEFAULT_CAPTURES = (
    {"name": "dashboard-dark", "page": "dashboard", "theme": "dark", "filename": "01-dashboard-dark.png", "actions": []},
    {"name": "dashboard-light", "page": "dashboard", "theme": "light", "filename": "02-dashboard-light.png", "actions": []},
    {"name": "users-dark", "page": "users", "theme": "dark", "filename": "03-users-dark.png", "actions": []},
    {"name": "users-light", "page": "users", "theme": "light", "filename": "04-users-light.png", "actions": []},
    {"name": "live-dark", "page": "live", "theme": "dark", "filename": "05-live-dark.png", "actions": []},
    {"name": "favorites-dark", "page": "favorites", "theme": "dark", "filename": "06-favorites-dark.png", "actions": []},
    {"name": "stats-dark", "page": "stats", "theme": "dark", "filename": "07-stats-dark.png", "actions": []},
    {"name": "analytics-dark", "page": "analytics", "theme": "dark", "filename": "08-analytics-dark.png", "actions": []},
    {"name": "admin-dark", "page": "admin", "theme": "dark", "filename": "09-admin-dark.png", "actions": []},
)

CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)

# These are the locations where the current TkLiveTracker frontend renders
# usernames. The general DOM anonymizer is broader; these selectors are used
# for the fail-closed verification step.
USERNAME_SELECTORS = (
    ".user-name",
    ".live-user-name strong",
    ".stream-user strong",
    ".favorite-user strong",
    ".username-link",
    ".username-link-button",
)

CAPTURE_SCROLLBAR_STYLE_ID = "__tklt-public-capture-scrollbars"
CAPTURE_SCROLLBAR_CSS = """
    html,
    body,
    * {
        scrollbar-width: none !important;
        -ms-overflow-style: none !important;
        scrollbar-gutter: stable !important;
    }

    *::-webkit-scrollbar {
        display: none !important;
        width: 0 !important;
        height: 0 !important;
    }
"""


@dataclass(frozen=True)
class CaptureResult:
    page: str
    theme: str
    filename: str
    anonymized_occurrences: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture anonymized public TkLiveTracker screenshots via Chrome/CDP."
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"TkLiveTracker web URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory. Default: /tmp/tklivetracker-public-screenshots-<timestamp>",
    )
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--settle",
        type=float,
        default=DEFAULT_SETTLE,
        help="Short visual settle delay after each page load (seconds).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window instead of using headless Chrome.",
    )
    parser.add_argument(
        "--show-scrollbars",
        action="store_true",
        help="Keep browser and nested-container scrollbars visible in captured PNGs.",
    )
    parser.add_argument(
        "--chrome-binary",
        type=Path,
        help="Explicit Chrome/Chromium executable. Normally auto-detected from PATH.",
    )
    parser.add_argument(
        "--debugger-address",
        metavar="HOST:PORT",
        help=(
            "Attach to an already running Chrome CDP instance, e.g. 127.0.0.1:9222. "
            "When used, this script does not create or close that browser process."
        ),
    )
    parser.add_argument(
        "--keep-profile",
        action="store_true",
        help="Keep the temporary Chrome profile after capture (normally deleted).",
    )
    parser.add_argument(
        "--recipes",
        type=Path,
        default=DEFAULT_RECIPES_PATH,
        help=(
            "Screenshot recipe YAML. Default: docs/screenshots/public_screenshots.yaml. "
            "The file is created with the built-in default captures if missing."
        ),
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help=(
            "Open a headed browser and interactively record screenshot states. "
            "Use Ctrl+Shift+S or the recorder button to save the current state."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List configured screenshot recipes and exit.",
    )
    parser.add_argument(
        "--remove",
        metavar="NAME_OR_FILENAME",
        help="Remove one screenshot recipe by recipe name or PNG filename and exit.",
    )
    parser.add_argument(
        "--publish",
        type=Path,
        metavar="REVIEWED_DIR",
        help=(
            "Publish a reviewed capture directory into docs/screenshots, delete stale managed "
            "PNGs, and create/update the generated Screenshots section in README.md. "
            "This mode does not start Chrome."
        ),
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "With --publish, create a local Git commit containing only README.md, the screenshot "
            "recipe YAML, and screenshot PNG additions/updates/deletions. Unrelated changes are "
            "left out of the commit."
        ),
    )
    parser.add_argument(
        "--commit-message",
        default="Update public screenshots",
        help="Commit message used with --publish --commit.",
    )
    return parser.parse_args()


def normalize_base_url(value: str) -> str:
    return value.rstrip("/") + "/"


def make_output_dir(requested: Path | None) -> Path:
    if requested is not None:
        output = requested.expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = Path(f"/tmp/tklivetracker-public-screenshots-{stamp}")
    output.mkdir(parents=True, exist_ok=False)
    return output


def recipe_path(path: Path) -> Path:
    return path.expanduser().resolve()


def _default_recipe_document() -> dict[str, Any]:
    return {
        "version": 1,
        "captures": [dict(item) for item in DEFAULT_CAPTURES],
    }


def save_recipes(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = yaml.safe_dump(
        document,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_recipes(path: Path, *, create_if_missing: bool = True) -> dict[str, Any]:
    path = recipe_path(path)
    if not path.exists():
        if not create_if_missing:
            raise RuntimeError(f"Recipe file does not exist: {path}")
        document = _default_recipe_document()
        save_recipes(path, document)
        return document

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Could not read screenshot recipe file {path}: {exc}") from exc

    if raw is None:
        raw = {"version": 1, "captures": []}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Screenshot recipe file must contain a YAML mapping: {path}")

    captures = raw.get("captures")
    if captures is None:
        captures = []
        raw["captures"] = captures
    if not isinstance(captures, list):
        raise RuntimeError("Screenshot recipe field 'captures' must be a list")

    for index, item in enumerate(captures, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Capture #{index} must be a mapping")
        for key in ("name", "page", "theme", "filename"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise RuntimeError(f"Capture #{index} has invalid or missing '{key}'")
        if item["theme"] not in {"dark", "light"}:
            raise RuntimeError(f"Capture '{item['name']}' has unsupported theme: {item['theme']}")
        actions = item.setdefault("actions", [])
        if not isinstance(actions, list):
            raise RuntimeError(f"Capture '{item['name']}' field 'actions' must be a list")
        filename = item["filename"]
        if not filename.lower().endswith(".png"):
            raise RuntimeError(f"Capture '{item['name']}' filename must end with .png")
        if Path(filename).name != filename or filename in {".", ".."}:
            raise RuntimeError(
                f"Capture '{item['name']}' filename must be a plain PNG basename, not a path"
            )

    raw.setdefault("version", 1)
    return raw


def list_recipes(document: dict[str, Any], path: Path) -> None:
    captures = document.get("captures", [])
    print(f"Screenshot recipes: {recipe_path(path)}")
    if not captures:
        print("(none)")
        return
    for index, item in enumerate(captures, start=1):
        print(
            f"{index:02d}. {item['filename']}  "
            f"name={item['name']} page={item['page']} theme={item['theme']} "
            f"actions={len(item.get('actions', []))}"
        )


def remove_recipe(document: dict[str, Any], key: str) -> bool:
    captures = document.get("captures", [])
    kept = [
        item
        for item in captures
        if item.get("name") != key and item.get("filename") != key
    ]
    if len(kept) == len(captures):
        return False
    document["captures"] = kept
    return True


def slugify_capture_name(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._")
    return value or "capture"


def unique_capture_identity(
    document: dict[str, Any],
    requested_name: str,
    theme: str,
) -> tuple[str, str]:
    captures = document.get("captures", [])
    existing_names = {str(item.get("name", "")) for item in captures}
    existing_files = {str(item.get("filename", "")) for item in captures}

    base = slugify_capture_name(requested_name)
    name = base
    suffix = 2
    while name in existing_names:
        name = f"{base}-{suffix}"
        suffix += 1

    next_index = len(captures) + 1
    stem = name if name.endswith(f"-{theme}") else f"{name}-{theme}"
    filename = f"{next_index:02d}-{stem}.png"
    while filename in existing_files:
        next_index += 1
        filename = f"{next_index:02d}-{stem}.png"
    return name, filename


def find_chrome(explicit: Path | None) -> str | None:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(f"Configured browser is not executable: {path}")
        return str(path)

    for name in CHROME_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found

    # A couple of common locations that might not be in PATH.
    for candidate in (
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/opt/google/chrome/google-chrome",
        "/opt/google/chrome/chrome",
    ):
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)

    return None


def check_server(base_url: str, timeout: float) -> None:
    try:
        response = requests.get(base_url, timeout=min(timeout, 5.0))
    except requests.RequestException as exc:
        raise RuntimeError(f"Cannot reach TkLiveTracker at {base_url}: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(
            f"TkLiveTracker returned HTTP {response.status_code} at {base_url}"
        )



def _png_is_valid(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def _capture_title(recipe: dict[str, Any]) -> str:
    value = str(recipe.get("name") or recipe.get("page") or "Screenshot")
    return value.replace("_", " ").replace("-", " ").strip().title()


def _page_title(page: str) -> str:
    return page.replace("_", " ").replace("-", " ").strip().title()


def build_readme_screenshot_section(document: dict[str, Any]) -> str:
    """Build a deterministic README Screenshots section from the recipe list."""
    captures = [item for item in document.get("captures", []) if isinstance(item, dict)]
    groups: dict[str, list[dict[str, Any]]] = {}
    for recipe in captures:
        groups.setdefault(str(recipe["page"]), []).append(recipe)

    lines = [
        README_SCREENSHOTS_BEGIN,
        "## Screenshots",
        "",
        (
            "The screenshots below are captured from a local TkLiveTracker instance with the "
            "public screenshot harness. Usernames are pseudonymized before each PNG is written."
        ),
        "",
    ]

    for page, recipes in groups.items():
        lines.extend([f"### {_page_title(page)}", ""])
        for offset in range(0, len(recipes), 2):
            pair = recipes[offset : offset + 2]
            if len(pair) == 1:
                recipe = pair[0]
                title = _capture_title(recipe)
                rel = f"docs/screenshots/{recipe['filename']}"
                lines.extend(
                    [
                        f"**{title}**",
                        "",
                        f"[![{title}]({rel})]({rel})",
                        "",
                    ]
                )
                continue

            titles = [_capture_title(recipe) for recipe in pair]
            rels = [f"docs/screenshots/{recipe['filename']}" for recipe in pair]
            lines.extend(
                [
                    f"| {titles[0]} | {titles[1]} |",
                    "| --- | --- |",
                    (
                        f"| [![{titles[0]}]({rels[0]})]({rels[0]}) "
                        f"| [![{titles[1]}]({rels[1]})]({rels[1]}) |"
                    ),
                    "",
                ]
            )

    lines.append(README_SCREENSHOTS_END)
    return "\n".join(lines).rstrip() + "\n"


def update_readme_screenshots(readme_path: Path, document: dict[str, Any]) -> bool:
    readme_path = readme_path.expanduser().resolve()
    if not readme_path.exists():
        raise RuntimeError(f"README does not exist: {readme_path}")

    original = readme_path.read_text(encoding="utf-8")
    section = build_readme_screenshot_section(document).rstrip()

    start = original.find(README_SCREENSHOTS_BEGIN)
    end = original.find(README_SCREENSHOTS_END)
    if start >= 0 or end >= 0:
        if start < 0 or end < 0 or end < start:
            raise RuntimeError(
                "README contains only one screenshot-section marker; refusing to rewrite it"
            )
        end += len(README_SCREENSHOTS_END)
        updated = original[:start].rstrip() + "\n\n" + section + "\n\n" + original[end:].lstrip()
    else:
        insertion_markers = ("## Testing", "## Project structure", "## Security")
        insert_at = -1
        for marker in insertion_markers:
            candidate = original.find(marker)
            if candidate >= 0:
                insert_at = candidate
                break
        if insert_at >= 0:
            updated = original[:insert_at].rstrip() + "\n\n" + section + "\n\n" + original[insert_at:]
        else:
            updated = original.rstrip() + "\n\n" + section + "\n"

    if updated == original:
        return False

    tmp = readme_path.with_name(readme_path.name + ".tmp")
    tmp.write_text(updated, encoding="utf-8")
    os.replace(tmp, readme_path)
    return True


def sync_public_screenshots(
    reviewed_dir: Path,
    public_dir: Path,
    document: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    """Mirror the reviewed recipe PNG set into the tracked public directory.

    The directory is dedicated to managed public screenshots. PNGs no longer
    referenced by the recipe YAML are deleted so removing a recipe also removes
    its previously published image on the next publish.
    """
    source = reviewed_dir.expanduser().resolve()
    destination = public_dir.expanduser().resolve()

    if not source.is_dir():
        raise RuntimeError(f"Reviewed screenshot directory does not exist: {source}")

    captures = [item for item in document.get("captures", []) if isinstance(item, dict)]
    expected = [str(item["filename"]) for item in captures]
    if not expected:
        raise RuntimeError("No screenshot recipes are configured; refusing to publish")

    missing: list[str] = []
    invalid: list[str] = []
    for filename in expected:
        candidate = source / filename
        if not candidate.is_file():
            missing.append(filename)
        elif not _png_is_valid(candidate):
            invalid.append(filename)

    if missing:
        raise RuntimeError(
            "Reviewed directory is incomplete; missing expected PNG(s): " + ", ".join(missing)
        )
    if invalid:
        raise RuntimeError(
            "Reviewed directory contains invalid PNG file(s): " + ", ".join(invalid)
        )

    destination.mkdir(parents=True, exist_ok=True)

    expected_set = set(expected)
    stale = sorted(
        path.name
        for path in destination.glob("*.png")
        if path.is_file() and path.name not in expected_set
    )

    added: list[str] = []
    updated: list[str] = []
    for filename in expected:
        src = source / filename
        dst = destination / filename

        if dst.exists() and dst.read_bytes() == src.read_bytes():
            continue

        existed = dst.exists()
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        (updated if existed else added).append(filename)

    for filename in stale:
        (destination / filename).unlink()

    return added, updated, stale


def _git_root_for(path: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Could not locate the Git repository for screenshot publishing") from exc
    return Path(result.stdout.strip()).resolve()


def _relative_to_repo(path: Path, repo_root: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"Path is outside the Git repository: {resolved}") from exc


def commit_public_screenshot_update(
    repo_root: Path,
    readme_path: Path,
    public_dir: Path,
    recipes_path: Path,
    message: str,
) -> str | None:
    """Commit only managed screenshot publication paths.

    `git commit --only` intentionally excludes any unrelated staged/unstaged
    work already present in the repository.
    """
    readme_rel = _relative_to_repo(readme_path, repo_root)
    public_rel = _relative_to_repo(public_dir, repo_root)
    recipes_rel = _relative_to_repo(recipes_path, repo_root)

    pathspecs = list(dict.fromkeys([readme_rel, public_rel, recipes_rel]))

    subprocess.run(
        ["git", "-C", str(repo_root), "add", "-A", "--", *pathspecs],
        check=True,
    )

    staged = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--cached", "--name-status", "--", *pathspecs],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    if not staged:
        return None

    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "commit",
            "--only",
            "-m",
            message,
            "--",
            *pathspecs,
        ],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


def publish_reviewed_capture(
    reviewed_dir: Path,
    recipes_path: Path,
    document: dict[str, Any],
    *,
    commit: bool,
    commit_message: str,
) -> int:
    public_dir = DEFAULT_PUBLIC_SCREENSHOT_DIR
    readme_path = DEFAULT_README_PATH

    # The recipe manifest belongs with the public screenshot set. A custom
    # --recipes path remains supported, but the normal project layout is
    # docs/screenshots/public_screenshots.yaml.
    added, updated, deleted = sync_public_screenshots(reviewed_dir, public_dir, document)
    readme_changed = update_readme_screenshots(readme_path, document)

    print("TkLiveTracker screenshot publication")
    print(f"Reviewed source: {reviewed_dir.expanduser().resolve()}")
    print(f"Public directory: {public_dir}")
    print(f"README: {readme_path}")
    print()
    print(f"Added PNGs:   {len(added)}")
    for name in added:
        print(f"  + {name}")
    print(f"Updated PNGs: {len(updated)}")
    for name in updated:
        print(f"  M {name}")
    print(f"Deleted PNGs: {len(deleted)}")
    for name in deleted:
        print(f"  D {name}")
    print(f"README changed: {'yes' if readme_changed else 'no'}")

    if not commit:
        print()
        print("Publication files were updated but not committed.")
        print("Review with:")
        print("  git diff -- README.md docs/screenshots")
        print("Then commit manually, or rerun --publish with --commit.")
        return 0

    repo_root = _git_root_for(REPO_ROOT)
    sha = commit_public_screenshot_update(
        repo_root,
        readme_path,
        public_dir,
        recipes_path,
        commit_message,
    )
    print()
    if sha is None:
        print("No managed screenshot/README changes to commit.")
    else:
        print(f"Created local commit: {sha} {commit_message}")
        print("Only README.md, screenshot PNG changes/deletions, and the recipe YAML were committed.")
        print("No push was performed.")
    return 0

def load_all_usernames(base_url: str, timeout: float) -> list[str]:
    """Read all usernames through the read-only web API.

    This is used only to build an in-memory denylist/pseudonym map. Values are
    deliberately never printed or written to disk.
    """
    usernames: list[str] = []
    page = 1

    while True:
        endpoint = urljoin(base_url, "api/users")
        params = {
            "page": page,
            "per_page": 100,
            "active_filter": "all",
            "live_filter": "all",
            "sort_by": "username",
            "sort_order": "asc",
        }
        try:
            response = requests.get(endpoint, params=params, timeout=min(timeout, 10.0))
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError("Could not obtain the username denylist from /api/users") from exc

        for user in payload.get("users", []):
            username = user.get("username")
            if isinstance(username, str) and username:
                usernames.append(username)

        pagination = payload.get("pagination") or {}
        if not pagination.get("has_next"):
            break
        page += 1

        # Defensive limit against a malformed pagination response.
        if page > 1_000_000:
            raise RuntimeError("Refusing to follow an unreasonable /api/users pagination loop")

    # Preserve exact spelling, eliminate duplicates, stable ordering.
    return sorted(set(usernames), key=lambda value: (value.casefold(), value))


def build_alias_map(usernames: Iterable[str]) -> dict[str, str]:
    """Create stable-within-this-run, random-looking aliases.

    The run key is ephemeral; the real->alias mapping is never persisted.
    """
    run_key = secrets.token_bytes(16)
    real_casefold = {name.casefold() for name in usernames}
    used: set[str] = set()
    result: dict[str, str] = {}

    for username in usernames:
        counter = 0
        while True:
            material = username.encode("utf-8") + counter.to_bytes(4, "big")
            digest = hashlib.blake2s(material, key=run_key, digest_size=8).digest()
            number = int.from_bytes(digest, "big") % 10_000_000
            alias = f"user{number:07d}"
            folded = alias.casefold()
            if folded not in used and folded not in real_casefold:
                break
            counter += 1
        result[username] = alias
        used.add(alias.casefold())

    return result


def create_driver(args: argparse.Namespace, profile_dir: Path | None) -> webdriver.Chrome:
    options = Options()

    if args.debugger_address:
        options.debugger_address = args.debugger_address
    else:
        browser = find_chrome(args.chrome_binary)
        if browser is None:
            raise RuntimeError(
                "Chrome/Chromium was not found. Install Chromium or pass "
                "--chrome-binary /path/to/browser. On CachyOS/Arch the package is normally 'chromium'."
            )
        options.binary_location = browser

        if not args.headed and not args.record:
            options.add_argument("--headless=new")
        assert profile_dir is not None
        options.add_argument(f"--user-data-dir={profile_dir}")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-features=Translate,MediaRouter")
        options.add_argument(f"--window-size={args.width},{args.height}")

    try:
        return webdriver.Chrome(options=options)
    except WebDriverException as exc:
        raise RuntimeError(f"Could not start/connect to Chrome: {exc.msg}") from exc


def set_viewport(driver: webdriver.Chrome, width: int, height: int) -> None:
    driver.execute_cdp_cmd(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": width,
            "height": height,
            "deviceScaleFactor": 1,
            "mobile": False,
            "screenWidth": width,
            "screenHeight": height,
        },
    )


def wait_for_app(driver: webdriver.Chrome, timeout: float) -> None:
    wait = WebDriverWait(driver, timeout)
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    wait.until(
        lambda d: bool(
            d.execute_script(
                """return !!window.app && !!document.querySelector('.nav-item[data-page="dashboard"]')"""
            )
        )
    )


def freeze_realtime(driver: webdriver.Chrome) -> None:
    driver.execute_script(
        """
        if (window.app) {
            if (typeof window.app.stopAutoRefresh === 'function') {
                window.app.stopAutoRefresh();
            }
            if (window.app.eventSource) {
                try { window.app.eventSource.close(); } catch (_) {}
                window.app.eventSource = null;
            }
            // Prevent code paths from reopening SSE during this capture session.
            window.app.setupServerSentEvents = function() {};
        }
        """
    )


def install_json_anonymizer(driver: webdriver.Chrome, aliases: dict[str, str]) -> None:
    """Wrap fetch and install O(text-length) username substitution helpers.

    The previous implementation iterated over every known username for every
    string. With 10k+ users that becomes quadratic and can stall Chrome on the
    Users page. This version tokenizes username-shaped substrings once and does
    O(1) map lookups.
    """
    driver.execute_script(
        r"""
        const aliases = arguments[0];

        // Keep the mapping in the page for the rest of the capture. It is
        // supplied once per run; later DOM scrubs and verification reuse it.
        window.__tkltPublicCaptureAliases = aliases;

        const folded = Object.create(null);
        for (const [real, alias] of Object.entries(aliases)) {
            const key = real.toLowerCase();
            if (!(key in folded)) folded[key] = alias;
        }
        window.__tkltPublicCaptureAliasesFolded = folded;

        const USER_TOKEN_RE = /[A-Za-z0-9._]+/g;

        function lookupAlias(token) {
            return aliases[token] || folded[token.toLowerCase()] || null;
        }

        function replaceIdentityText(value) {
            if (typeof value !== 'string' || !value) return value;
            return value.replace(USER_TOKEN_RE, token => lookupAlias(token) || token);
        }

        window.__tkltPublicCaptureLookupAlias = lookupAlias;
        window.__tkltPublicCaptureReplaceIdentityText = replaceIdentityText;

        function rewrite(value) {
            if (typeof value === 'string') return replaceIdentityText(value);
            if (Array.isArray(value)) return value.map(rewrite);
            if (value && typeof value === 'object') {
                const out = {};
                for (const [key, child] of Object.entries(value)) out[key] = rewrite(child);
                return out;
            }
            return value;
        }

        if (window.__tkltPublicCaptureFetchInstalled) return;

        window.__tkltPublicCaptureOriginalFetch = window.fetch.bind(window);
        window.fetch = async function(...fetchArgs) {
            const response = await window.__tkltPublicCaptureOriginalFetch(...fetchArgs);
            const contentType = response.headers.get('content-type') || '';
            if (!contentType.toLowerCase().includes('application/json')) return response;

            let payload;
            try {
                payload = await response.clone().json();
            } catch (_) {
                return response;
            }

            const rewritten = rewrite(payload);
            const headers = new Headers(response.headers);
            headers.delete('content-length');
            return new Response(JSON.stringify(rewritten), {
                status: response.status,
                statusText: response.statusText,
                headers
            });
        };

        window.__tkltPublicCaptureFetchInstalled = true;
        """,
        aliases,
    )

def navigate_and_wait(
    driver: webdriver.Chrome,
    page: str,
    timeout: float,
    settle: float,
) -> None:
    driver.execute_script("window.app.navigateTo(arguments[0]);", page)
    wait = WebDriverWait(driver, timeout)
    wait.until(
        lambda d: d.execute_script(
            "return window.app && window.app.currentPage === arguments[0]", page
        )
    )
    wait.until(
        lambda d: bool(
            d.execute_script(
                """
                const page = document.getElementById(arguments[0] + '-page');
                return !!page && page.classList.contains('active') && page.getAttribute('aria-hidden') !== 'true';
                """,
                page,
            )
        )
    )
    wait.until(
        lambda d: int(
            d.execute_script("return window.app ? window.app.pendingRequestCount : 999")
        )
        == 0
    )

    # Analytics uses canvas rendering after the request completes. A small,
    # deterministic settle also makes DOM/layout screenshots less timing-sensitive.
    time.sleep(max(0.0, settle))

    # No background refresh should be allowed to reintroduce real data.
    freeze_realtime(driver)


def set_theme(driver: webdriver.Chrome, theme: str) -> None:
    if theme not in {"dark", "light"}:
        raise ValueError(f"Unsupported theme: {theme}")
    driver.execute_script(
        """
        const theme = arguments[0];
        try { localStorage.setItem('theme', theme); } catch (_) {}
        if (window.app) window.app.currentTheme = theme;
        if (theme === 'dark') {
            document.documentElement.setAttribute('data-theme', 'dark');
        } else {
            document.documentElement.removeAttribute('data-theme');
        }
        const toggle = document.getElementById('theme-toggle');
        if (toggle) {
            toggle.title = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
        }
        """,
        theme,
    )


def install_recording_helper(driver: webdriver.Chrome, aliases: dict[str, str]) -> None:
    """Install the headed recorder UI and action logger.

    The recorder deliberately does not anonymize the page the user is looking at.
    The alias map is present only so locator generation can reject text containing
    known real usernames. Recipes therefore remain safe to commit.
    """
    driver.execute_script(
        r"""
        const aliases = arguments[0] || {};
        const known = Object.create(null);
        for (const real of Object.keys(aliases)) known[real.toLowerCase()] = true;
        const USER_TOKEN_RE = /[A-Za-z0-9._]+/g;

        function containsKnownUsername(value) {
            if (!value) return false;
            const tokens = String(value).match(USER_TOKEN_RE) || [];
            return tokens.some(token => known[token.toLowerCase()]);
        }

        function visibleText(el) {
            return (el?.innerText || el?.textContent || '').replace(/\s+/g, ' ').trim();
        }

        function cssEscape(value) {
            if (window.CSS && typeof CSS.escape === 'function') return CSS.escape(value);
            return String(value).replace(/[^A-Za-z0-9_-]/g, ch => '\\' + ch);
        }

        function uniqueIndex(selector, element) {
            const nodes = Array.from(document.querySelectorAll(selector));
            const index = nodes.indexOf(element);
            return index >= 0 ? index : 0;
        }

        function locatorFor(element) {
            if (!element || !(element instanceof Element)) return null;

            const screenshotId = element.getAttribute('data-screenshot-id');
            if (screenshotId && !containsKnownUsername(screenshotId)) {
                const selector = `[data-screenshot-id="${String(screenshotId).replace(/"/g, '\\"')}"]`;
                return {by: 'screenshot_id', value: screenshotId, index: uniqueIndex(selector, element)};
            }

            if (element.id && !containsKnownUsername(element.id)) {
                return {by: 'id', value: element.id};
            }

            const aria = element.getAttribute('aria-label');
            if (aria && !containsKnownUsername(aria)) {
                const tag = element.tagName.toLowerCase();
                const selector = `${tag}[aria-label="${String(aria).replace(/"/g, '\\"')}"]`;
                return {by: 'aria', tag, value: aria, index: uniqueIndex(selector, element)};
            }

            const role = element.getAttribute('role');
            const text = visibleText(element);
            if (role && text && text.length <= 120 && !containsKnownUsername(text)) {
                const nodes = Array.from(document.querySelectorAll(`[role="${String(role).replace(/"/g, '\\"')}"]`))
                    .filter(node => visibleText(node) === text);
                return {by: 'role_text', role, value: text, index: Math.max(0, nodes.indexOf(element))};
            }

            if (['BUTTON', 'A', 'SUMMARY', 'LABEL'].includes(element.tagName) &&
                text && text.length <= 120 && !containsKnownUsername(text)) {
                const tag = element.tagName.toLowerCase();
                const nodes = Array.from(document.querySelectorAll(tag)).filter(node => visibleText(node) === text);
                return {by: 'text', tag, value: text, index: Math.max(0, nodes.indexOf(element))};
            }

            // Safe structural fallback. Do not include text or data values that may
            // contain user identifiers. Prefer stable classes and nth-of-type.
            const parts = [];
            let node = element;
            for (let depth = 0; node && node !== document.body && depth < 5; depth += 1, node = node.parentElement) {
                let part = node.tagName.toLowerCase();
                if (node.id && !containsKnownUsername(node.id)) {
                    part += `#${cssEscape(node.id)}`;
                    parts.unshift(part);
                    break;
                }
                const classes = Array.from(node.classList || [])
                    .filter(cls => /^[A-Za-z_][A-Za-z0-9_-]*$/.test(cls) && !containsKnownUsername(cls))
                    .slice(0, 2);
                if (classes.length) part += '.' + classes.map(cssEscape).join('.');
                const parent = node.parentElement;
                if (parent) {
                    const sameTag = Array.from(parent.children).filter(child => child.tagName === node.tagName);
                    if (sameTag.length > 1) part += `:nth-of-type(${sameTag.indexOf(node) + 1})`;
                }
                parts.unshift(part);
            }
            const css = parts.join(' > ');
            if (!css || containsKnownUsername(css)) return null;
            return {by: 'css', value: css};
        }

        function currentTheme() {
            return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
        }

        function currentPage() {
            return (window.app && window.app.currentPage) || 'dashboard';
        }

        function serializeScroll() {
            const elements = [];
            for (const el of document.querySelectorAll('*')) {
                if (!el.scrollTop && !el.scrollLeft) continue;
                if (!el.id && !el.getAttribute('data-screenshot-id')) continue;
                const locator = locatorFor(el);
                if (!locator) continue;
                elements.push({locator, x: el.scrollLeft || 0, y: el.scrollTop || 0});
            }
            return {
                window: {x: window.scrollX || 0, y: window.scrollY || 0},
                elements,
            };
        }

        if (!window.__tkltRecorder) {
            window.__tkltRecorder = {
                baselinePage: currentPage(),
                actions: [],
                queue: [],
            };
        }
        const state = window.__tkltRecorder;
        state.baselinePage = state.baselinePage || currentPage();
        state.namingOpen = false;
        state.pendingCapture = null;
        state.previousBodyOverflow = '';

        function pushAction(action) {
            state.actions.push(action);
            updateBadge();
        }

        function updateBadge() {
            const badge = document.getElementById('__tklt-recorder-badge');
            if (badge) {
                badge.textContent = `Recorder: ${state.baselinePage} · ${state.actions.length} action${state.actions.length === 1 ? '' : 's'}`;
            }
        }

        function snapshotCaptureState() {
            return {
                page: state.baselinePage || currentPage(),
                currentPage: currentPage(),
                theme: currentTheme(),
                actions: JSON.parse(JSON.stringify(state.actions)),
                scroll: serializeScroll(),
            };
        }

        function openSavePanel() {
            const overlay = document.getElementById('__tklt-recorder-modal-overlay');
            const input = document.getElementById('__tklt-recorder-name');
            if (!overlay || !input || state.namingOpen) return;

            // Critical recorder invariant: Ctrl+Shift+S captures the state *now*.
            // Naming/saving the recipe must not change the recorded action list or
            // scroll position, even if the user later interacts with the page.
            state.pendingCapture = snapshotCaptureState();
            state.namingOpen = true;
            state.previousBodyOverflow = document.body.style.overflow;
            document.body.style.overflow = 'hidden';

            const suggestion = `${currentPage()}-${currentTheme()}`;
            input.value = suggestion;
            overlay.style.display = 'flex';
            input.focus();
            input.select();
        }

        function closeSavePanel(discardPending = true) {
            const overlay = document.getElementById('__tklt-recorder-modal-overlay');
            if (overlay) overlay.style.display = 'none';
            document.body.style.overflow = state.previousBodyOverflow || '';
            state.namingOpen = false;
            if (discardPending) state.pendingCapture = null;
        }

        function saveCapture() {
            const input = document.getElementById('__tklt-recorder-name');
            const name = (input?.value || '').trim();
            if (!name || !state.pendingCapture) return;

            state.queue.push({
                name,
                ...state.pendingCapture,
            });
            state.pendingCapture = null;
            closeSavePanel(false);
            const flash = document.getElementById('__tklt-recorder-flash');
            if (flash) {
                flash.textContent = `Saved recipe: ${name}`;
                flash.style.opacity = '1';
                setTimeout(() => { flash.style.opacity = '0'; }, 1500);
            }
        }

        if (!document.getElementById('__tklt-recorder-ui')) {
            const root = document.createElement('div');
            root.id = '__tklt-recorder-ui';
            root.setAttribute('data-tklt-recorder-ui', '1');
            root.innerHTML = `
                <style>
                    #__tklt-recorder-ui { position: fixed; right: 16px; bottom: 16px; z-index: 2147483647; font: 13px/1.4 system-ui, sans-serif; color: #fff; }
                    #__tklt-recorder-controls { display:flex; gap:8px; align-items:center; background:#111827; border:1px solid #374151; border-radius:10px; padding:8px 10px; box-shadow:0 8px 28px rgba(0,0,0,.35); }
                    #__tklt-recorder-controls button, #__tklt-recorder-panel button { font:inherit; cursor:pointer; border:1px solid #4b5563; border-radius:6px; background:#1f2937; color:#fff; padding:8px 11px; }
                    #__tklt-recorder-controls button:hover, #__tklt-recorder-panel button:hover { background:#374151; }
                    #__tklt-recorder-modal-overlay { display:none; position:fixed; inset:0; z-index:2147483646; align-items:center; justify-content:center; padding:24px; background:rgba(2,6,23,.58); box-sizing:border-box; }
                    #__tklt-recorder-panel { width:min(90vw, 520px); max-height:calc(100vh - 48px); overflow:auto; background:#111827; border:1px solid #374151; border-radius:12px; padding:18px; box-shadow:0 18px 60px rgba(0,0,0,.5); box-sizing:border-box; }
                    #__tklt-recorder-panel label { display:block; margin-bottom:8px; font-size:15px; }
                    #__tklt-recorder-name { box-sizing:border-box; width:100%; padding:10px 12px; margin-bottom:14px; border:1px solid #4b5563; border-radius:8px; background:#0f172a; color:#fff; font:inherit; font-size:15px; }
                    #__tklt-recorder-panel-actions { display:flex; justify-content:flex-end; gap:8px; }
                    #__tklt-recorder-flash { position:absolute; right:0; bottom:48px; white-space:nowrap; background:#065f46; border-radius:6px; padding:6px 9px; opacity:0; transition:opacity .15s; pointer-events:none; }
                </style>
                <div id="__tklt-recorder-flash"></div>
                <div id="__tklt-recorder-controls">
                    <span id="__tklt-recorder-badge"></span>
                    <button id="__tklt-recorder-save" type="button">Save state · Ctrl+Shift+S</button>
                </div>
                <div id="__tklt-recorder-modal-overlay">
                    <div id="__tklt-recorder-panel" role="dialog" aria-modal="true" aria-labelledby="__tklt-recorder-dialog-title">
                        <label id="__tklt-recorder-dialog-title" for="__tklt-recorder-name">Screenshot recipe name</label>
                        <input id="__tklt-recorder-name" autocomplete="off" spellcheck="false">
                        <div id="__tklt-recorder-panel-actions">
                            <button id="__tklt-recorder-cancel" type="button">Cancel</button>
                            <button id="__tklt-recorder-confirm" type="button">Save recipe</button>
                        </div>
                    </div>
                </div>`;
            document.body.appendChild(root);
            document.getElementById('__tklt-recorder-save').addEventListener('click', openSavePanel);
            document.getElementById('__tklt-recorder-cancel').addEventListener('click', () => closeSavePanel(true));
            document.getElementById('__tklt-recorder-confirm').addEventListener('click', saveCapture);
            document.getElementById('__tklt-recorder-name').addEventListener('keydown', event => {
                if (event.key === 'Enter') { event.preventDefault(); saveCapture(); }
                if (event.key === 'Escape') { event.preventDefault(); closeSavePanel(true); }
            });
        }
        updateBadge();

        if (!window.__tkltRecorderListenersInstalled) {
            document.addEventListener('keydown', event => {
                if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === 's') {
                    event.preventDefault();
                    event.stopPropagation();
                    openSavePanel();
                }
            }, true);

            document.addEventListener('click', event => {
                if (state.namingOpen) return;
                const raw = event.target instanceof Element ? event.target : null;
                if (!raw || raw.closest('[data-tklt-recorder-ui="1"]')) return;

                const target = raw.closest('[data-screenshot-id], button, a, [role="button"], [role="tab"], summary, label');
                if (!target) return;

                // Top-level SPA navigation is a clean baseline. We do not need to
                // replay every sidebar click before reaching the state of interest.
                const nav = target.closest('.nav-item[data-page]');
                if (nav) {
                    const page = nav.getAttribute('data-page');
                    if (page) {
                        state.baselinePage = page;
                        state.actions = [];
                        setTimeout(updateBadge, 0);
                    }
                    return;
                }

                if (target.id === 'theme-toggle') return;

                const locator = locatorFor(target);
                if (!locator) return;
                pushAction({type: 'click', locator});
            }, true);

            document.addEventListener('change', event => {
                if (state.namingOpen) return;
                const target = event.target;
                if (!(target instanceof HTMLSelectElement || target instanceof HTMLInputElement)) return;
                if (target.closest('[data-tklt-recorder-ui="1"]')) return;
                const locator = locatorFor(target);
                if (!locator) return;

                if (target instanceof HTMLSelectElement) {
                    pushAction({type: 'select', locator, value: target.value});
                    return;
                }
                if (['checkbox', 'radio'].includes(target.type)) {
                    pushAction({type: 'check', locator, checked: !!target.checked});
                }
                // Text/password inputs are intentionally not recorded. They may
                // contain usernames, tokens or other private data.
            }, true);

            window.__tkltRecorderListenersInstalled = true;
        }
        """,
        aliases,
    )


def pop_recorded_captures(driver: webdriver.Chrome) -> list[dict[str, Any]]:
    result = driver.execute_script(
        """
        const state = window.__tkltRecorder;
        if (!state || !Array.isArray(state.queue) || !state.queue.length) return [];
        return state.queue.splice(0, state.queue.length);
        """
    )
    return result if isinstance(result, list) else []


def current_browser_page(driver: webdriver.Chrome) -> str:
    value = driver.execute_script("return (window.app && window.app.currentPage) || 'dashboard';")
    return str(value or "dashboard")


def current_browser_theme(driver: webdriver.Chrome) -> str:
    value = driver.execute_script(
        "return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';"
    )
    return str(value)


def run_recorder(
    driver: webdriver.Chrome,
    aliases: dict[str, str],
    document: dict[str, Any],
    recipes_path: Path,
) -> None:
    install_recording_helper(driver, aliases)
    print()
    print("Interactive screenshot recipe recorder")
    print("- Browse TkLiveTracker normally in the Chrome window.")
    print("- Open a tab, modal, popup, filter or other state you want to capture.")
    print("- Press Ctrl+Shift+S, or click the recorder button in the bottom-right corner.")
    print("- Give the state a short name, e.g. analytics-new-users or users-edit-modal.")
    print("- Close the Chrome window when finished; the recorder will exit automatically.")
    print(f"- Recipes are saved immediately to: {recipes_path}")
    print()

    while True:
        try:
            if not driver.window_handles:
                break
            items = pop_recorded_captures(driver)
        except WebDriverException:
            break

        for item in items:
            requested_name = str(item.get("name") or "capture")
            theme = str(item.get("theme") or "dark")
            if theme not in {"dark", "light"}:
                theme = "dark"
            name, filename = unique_capture_identity(document, requested_name, theme)
            actions = item.get("actions") if isinstance(item.get("actions"), list) else []
            recipe: dict[str, Any] = {
                "name": name,
                "filename": filename,
                "page": str(item.get("page") or "dashboard"),
                "theme": theme,
                "actions": actions,
            }
            scroll = item.get("scroll")
            if isinstance(scroll, dict):
                recipe["scroll"] = scroll

            document.setdefault("captures", []).append(recipe)
            save_recipes(recipes_path, document)
            print(
                f"Saved recipe: {filename}  "
                f"page={recipe['page']} theme={theme} actions={len(actions)}"
            )
        time.sleep(0.20)


def _locator_script() -> str:
    return r"""
        const locator = arguments[0];
        function visibleText(el) {
            return (el?.innerText || el?.textContent || '').replace(/\s+/g, ' ').trim();
        }
        function pick(nodes, index) {
            const list = Array.from(nodes || []);
            return list[Math.max(0, Number(index || 0))] || null;
        }
        let el = null;
        if (locator.by === 'screenshot_id') {
            el = pick(document.querySelectorAll(`[data-screenshot-id="${CSS.escape(locator.value)}"]`), locator.index);
        } else if (locator.by === 'id') {
            el = document.getElementById(locator.value);
        } else if (locator.by === 'aria') {
            const tag = locator.tag || '*';
            el = pick(Array.from(document.querySelectorAll(`${tag}[aria-label]`)).filter(node => node.getAttribute('aria-label') === locator.value), locator.index);
        } else if (locator.by === 'role_text') {
            el = pick(Array.from(document.querySelectorAll(`[role="${CSS.escape(locator.role)}"]`)).filter(node => visibleText(node) === locator.value), locator.index);
        } else if (locator.by === 'text') {
            const tag = locator.tag || '*';
            el = pick(Array.from(document.querySelectorAll(tag)).filter(node => visibleText(node) === locator.value), locator.index);
        } else if (locator.by === 'css') {
            try { el = document.querySelector(locator.value); } catch (_) { el = null; }
        }
    """


def execute_recipe_action(
    driver: webdriver.Chrome,
    action: dict[str, Any],
    timeout: float,
    settle: float,
) -> None:
    action_type = action.get("type")
    locator = action.get("locator")
    if not isinstance(locator, dict):
        raise RuntimeError(f"Recipe action has no valid locator: {action}")

    finder = _locator_script()
    wait = WebDriverWait(driver, timeout)
    wait.until(lambda d: bool(d.execute_script(finder + "\nreturn !!el;", locator)))

    if action_type == "click":
        ok = driver.execute_script(
            finder + "\nif (!el) return false; el.scrollIntoView({block:'center', inline:'nearest'}); el.click(); return true;",
            locator,
        )
    elif action_type == "select":
        ok = driver.execute_script(
            finder + r"""
            if (!el) return false;
            el.value = arguments[1];
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            return true;
            """,
            locator,
            action.get("value"),
        )
    elif action_type == "check":
        ok = driver.execute_script(
            finder + r"""
            if (!el) return false;
            el.checked = !!arguments[1];
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            return true;
            """,
            locator,
            bool(action.get("checked")),
        )
    else:
        raise RuntimeError(f"Unsupported recipe action type: {action_type}")

    if not ok:
        raise RuntimeError(f"Could not replay recipe action: {action}")

    try:
        WebDriverWait(driver, timeout).until(
            lambda d: int(d.execute_script("return window.app ? window.app.pendingRequestCount : 0")) == 0
        )
    except TimeoutException:
        raise RuntimeError(f"Timed out after replaying recipe action: {action}")
    time.sleep(min(max(settle, 0.0), 1.0))


def apply_recorded_scroll(driver: webdriver.Chrome, scroll: Any) -> None:
    if not isinstance(scroll, dict):
        return
    window_scroll = scroll.get("window")
    if isinstance(window_scroll, dict):
        driver.execute_script(
            "window.scrollTo(arguments[0], arguments[1]);",
            int(window_scroll.get("x", 0) or 0),
            int(window_scroll.get("y", 0) or 0),
        )
    for item in scroll.get("elements", []) if isinstance(scroll.get("elements"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("locator"), dict):
            continue
        driver.execute_script(
            _locator_script() + "\nif (el) { el.scrollLeft = arguments[1]; el.scrollTop = arguments[2]; return true; } return false;",
            item["locator"],
            int(item.get("x", 0) or 0),
            int(item.get("y", 0) or 0),
        )


def prepare_recipe_state(
    driver: webdriver.Chrome,
    recipe: dict[str, Any],
    timeout: float,
    settle: float,
) -> None:
    navigate_and_wait(driver, str(recipe["page"]), timeout, settle)
    set_theme(driver, str(recipe["theme"]))
    reset_scroll(driver)
    for action in recipe.get("actions", []):
        if not isinstance(action, dict):
            raise RuntimeError(f"Recipe '{recipe['name']}' contains a non-mapping action")
        execute_recipe_action(driver, action, timeout, settle)
    apply_recorded_scroll(driver, recipe.get("scroll"))
    time.sleep(max(0.0, settle))


def anonymize_dom(driver: webdriver.Chrome) -> int:
    """Second-line DOM scrub after network-response anonymization.

    Uses the alias map already installed in the page. Runtime is proportional
    to visible DOM text rather than usernames x DOM nodes.
    """
    return int(
        driver.execute_script(
            r"""
            const aliases = window.__tkltPublicCaptureAliases || {};
            const folded = window.__tkltPublicCaptureAliasesFolded || {};
            let replacements = 0;
            const USER_TOKEN_RE = /[A-Za-z0-9._]+/g;

            function lookupAlias(token) {
                return aliases[token] || folded[token.toLowerCase()] || null;
            }

            function replaceIdentityText(value) {
                if (typeof value !== 'string' || !value) return value;
                return value.replace(USER_TOKEN_RE, token => {
                    const alias = lookupAlias(token);
                    if (!alias) return token;
                    replacements += 1;
                    return alias;
                });
            }

            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            const nodes = [];
            while (walker.nextNode()) nodes.push(walker.currentNode);
            for (const node of nodes) {
                const parent = node.parentElement;
                if (!parent || ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(parent.tagName)) continue;
                const next = replaceIdentityText(node.nodeValue || '');
                if (next !== node.nodeValue) node.nodeValue = next;
            }

            const attrs = ['title', 'aria-label', 'alt', 'placeholder', 'data-username'];
            for (const element of document.querySelectorAll('*')) {
                for (const attr of attrs) {
                    if (!element.hasAttribute(attr)) continue;
                    const oldValue = element.getAttribute(attr) || '';
                    const newValue = replaceIdentityText(oldValue);
                    if (newValue !== oldValue) element.setAttribute(attr, newValue);
                }
                if ((element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) && element.value) {
                    element.value = replaceIdentityText(element.value);
                }
            }

            // Public screenshots should not contain transient notification text.
            // Deliberately keep open dialogs/modals: recipe recording is allowed
            // to target those UI states.
            document.querySelectorAll('.toast, .toast-container').forEach(el => el.remove());

            return replacements;
            """
        )
    )

def redact_admin_paths(driver: webdriver.Chrome) -> None:
    driver.execute_script(
        """
        const replacements = {
            'recordings-path': '/srv/tklivetracker/recordings',
            'log-path': '/srv/tklivetracker/logs',
            'db-path': '/srv/tklivetracker/db.sqlite'
        };
        for (const [id, value] of Object.entries(replacements)) {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        }
        """
    )


def install_capture_css(driver: webdriver.Chrome) -> None:
    driver.execute_script(
        """
        let style = document.getElementById('__tklt-public-capture-style');
        if (!style) {
            style = document.createElement('style');
            style.id = '__tklt-public-capture-style';
            style.textContent = `
                *, *::before, *::after {
                    animation: none !important;
                    transition: none !important;
                    caret-color: transparent !important;
                }
                html { scroll-behavior: auto !important; }
            `;
            document.head.appendChild(style);
        }
        if (window.Chart && window.Chart.defaults) {
            window.Chart.defaults.animation = false;
        }
        """
    )


def verify_anonymization(
    driver: webdriver.Chrome,
    page: str,
) -> None:
    """Fail closed if username-bearing UI still exposes a real username.

    Verification reuses the browser-side alias map instead of transferring the
    10k+ username denylist for every screenshot. It checks username-shaped
    tokens, so compound labels such as "@name · live" are covered too.
    """
    result = driver.execute_script(
        r"""
        const aliases = window.__tkltPublicCaptureAliases || {};
        const folded = window.__tkltPublicCaptureAliasesFolded || {};
        const selectors = arguments[0];
        const USER_TOKEN_RE = /[A-Za-z0-9._]+/g;
        let failures = 0;
        let checked = 0;

        function isRealUsernameToken(token) {
            return !!(aliases[token] || folded[token.toLowerCase()]);
        }

        function checkValue(value) {
            if (!value) return;
            const tokens = String(value).match(USER_TOKEN_RE) || [];
            for (const token of tokens) {
                checked += 1;
                if (isRealUsernameToken(token)) failures += 1;
            }
        }

        for (const selector of selectors) {
            for (const el of document.querySelectorAll(selector)) {
                checkValue((el.textContent || '').trim());
                checkValue((el.getAttribute('data-username') || '').trim());
            }
        }
        return { failures, checked };
        """,
        list(USERNAME_SELECTORS),
    )
    if int(result.get("failures", 0)):
        raise RuntimeError(
            f"Anonymization verification failed on page '{page}': "
            f"{result['failures']} username token(s) still contain real identifiers. "
            "Screenshot was not written."
        )

    # Admin can expose local filesystem paths. They are explicitly replaced;
    # this check makes a missed local home/repository path fail closed as well.
    if page == "admin":
        sensitive_prefixes = [str(Path.home())]
        try:
            sensitive_prefixes.append(str(Path.cwd().resolve()))
        except OSError:
            pass
        visible_text = str(driver.execute_script("return document.body.innerText || ''"))
        if any(prefix and prefix in visible_text for prefix in sensitive_prefixes):
            raise RuntimeError(
                "Admin anonymization verification failed: a local filesystem path is still visible. "
                "Screenshot was not written."
            )

def reset_scroll(driver: webdriver.Chrome) -> None:
    driver.execute_script(
        """
        window.scrollTo(0, 0);
        document.querySelectorAll('.content-area, .page, .table-container').forEach(el => {
            try { el.scrollTop = 0; el.scrollLeft = 0; } catch (_) {}
        });
        """
    )


def configure_capture_scrollbars(driver: webdriver.Chrome, *, hide_scrollbars: bool) -> None:
    """Temporarily hide browser and nested scrollbars for a PNG capture."""
    driver.execute_script(
        """
        const styleId = arguments[0];
        const css = arguments[1];
        const hideScrollbars = arguments[2];
        const existing = document.getElementById(styleId);
        if (!hideScrollbars) {
            if (existing) existing.remove();
            return;
        }
        const style = existing || document.createElement('style');
        style.id = styleId;
        style.textContent = css;
        if (!existing) document.head.appendChild(style);
        """,
        CAPTURE_SCROLLBAR_STYLE_ID,
        CAPTURE_SCROLLBAR_CSS,
        hide_scrollbars,
    )


def capture_png(
    driver: webdriver.Chrome,
    output_path: Path,
    *,
    hide_scrollbars: bool,
) -> None:
    configure_capture_scrollbars(driver, hide_scrollbars=hide_scrollbars)
    payload = driver.execute_cdp_cmd(
        "Page.captureScreenshot",
        {
            "format": "png",
            "fromSurface": True,
            "captureBeyondViewport": False,
        },
    )
    data = payload.get("data")
    if not data:
        raise RuntimeError("Chrome returned an empty screenshot")
    output_path.write_bytes(base64.b64decode(data))


def write_report(
    output_dir: Path,
    base_url: str,
    width: int,
    height: int,
    username_count: int,
    results: list[CaptureResult],
) -> None:
    lines = [
        "TkLiveTracker public screenshot capture",
        "",
        f"Base URL: {base_url}",
        f"Viewport: {width}x{height} @ 1x",
        f"Known usernames anonymized in-memory: {username_count}",
        "Real username mapping: NOT SAVED",
        "",
        "Files:",
    ]
    for result in results:
        lines.append(
            f"- {result.filename}: page={result.page}, theme={result.theme}, "
            f"DOM replacements={result.anonymized_occurrences}"
        )
    lines.extend(
        [
            "",
            "Review every PNG manually before copying it into the public repository.",
        ]
    )
    (output_dir / "capture-report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.width < 320 or args.height < 320:
        print("error: viewport dimensions must be at least 320x320", file=sys.stderr)
        return 2
    if args.timeout <= 0 or args.settle < 0:
        print("error: --timeout must be positive and --settle cannot be negative", file=sys.stderr)
        return 2
    mode_count = (
        int(bool(args.record))
        + int(bool(args.list))
        + int(bool(args.remove))
        + int(bool(args.publish))
    )
    if mode_count > 1:
        print("error: --record, --list, --remove and --publish are mutually exclusive", file=sys.stderr)
        return 2
    if args.commit and not args.publish:
        print("error: --commit is only valid together with --publish", file=sys.stderr)
        return 2

    recipes_path = recipe_path(args.recipes)
    try:
        document = load_recipes(recipes_path)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.list:
        list_recipes(document, recipes_path)
        return 0

    if args.remove:
        if not remove_recipe(document, args.remove):
            print(f"error: no recipe named/filed '{args.remove}'", file=sys.stderr)
            return 2
        save_recipes(recipes_path, document)
        print(f"Removed recipe: {args.remove}")
        print(f"Updated: {recipes_path}")
        print("The next --publish run will also delete any now-stale published PNG and README entry.")
        return 0

    if args.publish:
        try:
            return publish_reviewed_capture(
                args.publish,
                recipes_path,
                document,
                commit=bool(args.commit),
                commit_message=str(args.commit_message),
            )
        except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    base_url = normalize_base_url(args.base_url)
    output_dir: Path | None = None
    if not args.record:
        try:
            output_dir = make_output_dir(args.output_dir)
        except FileExistsError:
            print("error: output directory already exists; refusing to overwrite it", file=sys.stderr)
            return 2

    profile_dir: Path | None = None
    driver: webdriver.Chrome | None = None
    attached = bool(args.debugger_address)

    if args.record:
        print("TkLiveTracker screenshot recipe recorder")
        print(f"Recipes: {recipes_path}")
        print(f"Viewport: {args.width}x{args.height} @ 1x")
    else:
        assert output_dir is not None
        print("TkLiveTracker public screenshot capture")
        print(f"Recipes: {recipes_path}")
        print(f"Output: {output_dir}")
        print(f"Viewport: {args.width}x{args.height} @ 1x")

    try:
        check_server(base_url, args.timeout)
        usernames = load_all_usernames(base_url, args.timeout)
        aliases = build_alias_map(usernames)
        print(f"Loaded {len(usernames)} usernames into an in-memory anonymization map")

        if not attached:
            profile_dir = Path(tempfile.mkdtemp(prefix="tklivetracker-capture-profile-", dir="/tmp"))

        driver = create_driver(args, profile_dir)
        driver.set_script_timeout(max(60.0, args.timeout))
        set_viewport(driver, args.width, args.height)
        driver.get(base_url)
        wait_for_app(driver, args.timeout)

        if args.record:
            run_recorder(driver, aliases, document, recipes_path)
            print()
            print("Recorder finished.")
            print(f"Recipes: {recipes_path}")
            print("Run the script again without --record to render all configured screenshots.")
            return 0

        captures = document.get("captures", [])
        if not captures:
            raise RuntimeError(f"No captures are configured in {recipes_path}")

        results: list[CaptureResult] = []
        for index, recipe in enumerate(captures, start=1):
            page = str(recipe["page"])
            theme = str(recipe["theme"])
            filename = str(recipe["filename"])
            name = str(recipe["name"])
            print(
                f"[{index}/{len(captures)}] {name} -> {filename} ... ",
                end="",
                flush=True,
            )

            # Start every recipe from a clean application document so a modal,
            # selected tab or other state from the previous recipe cannot leak
            # into the next capture. The alias map is never persisted.
            if index > 1:
                driver.get(base_url)
                wait_for_app(driver, args.timeout)
                set_viewport(driver, args.width, args.height)

            # IMPORTANT: replay recipe actions against the real application data.
            # Do not anonymize JSON responses before replay: actions such as
            # Users -> Edit use the real username as an application identifier.
            # Rewriting /api/users before the click would make the UI try to
            # edit a synthetic user (for example user1234567), so the modal
            # would never open.
            freeze_realtime(driver)
            install_capture_css(driver)
            prepare_recipe_state(driver, recipe, args.timeout, args.settle)

            # Only after the complete interaction recipe has been replayed do
            # we install the anonymization layer. From this point on no recipe
            # action depends on private identifiers. The fetch wrapper protects
            # against any late/background JSON response, while the DOM scrub
            # anonymizes the already-rendered state (including open modals).
            install_json_anonymizer(driver, aliases)

            # Freeze again after replay: actions may have caused a component to
            # reconnect or schedule another refresh.
            freeze_realtime(driver)
            install_capture_css(driver)
            replacements = anonymize_dom(driver)
            actual_page = current_browser_page(driver)
            if actual_page == "admin":
                redact_admin_paths(driver)
            time.sleep(0.12)

            verify_anonymization(driver, actual_page)
            capture_png(
                driver,
                output_dir / filename,
                hide_scrollbars=not args.show_scrollbars,
            )
            results.append(CaptureResult(actual_page, theme, filename, replacements))
            print("OK")

        write_report(
            output_dir,
            base_url,
            args.width,
            args.height,
            len(usernames),
            results,
        )

        print()
        print(f"Created {len(results)} screenshots")
        print(f"Review them in: {output_dir}")
        print("No real username mapping was written to disk.")
        return 0

    except (RuntimeError, TimeoutException, WebDriverException) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        if output_dir is not None:
            print(f"Partial output, if any, remains in: {output_dir}", file=sys.stderr)
        return 1
    finally:
        if driver is not None and not attached:
            try:
                driver.quit()
            except Exception:
                pass
        if profile_dir is not None and not args.keep_profile:
            shutil.rmtree(profile_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
