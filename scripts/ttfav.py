#!/usr/bin/env python3
import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "ttracker" / "fav.json"
API_TIMEOUT_SECONDS = 5.0
_TIKTOK_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._]{2,24}$")


@dataclass
class TtFavConfig:
    api_url: str


@dataclass
class TtFavAction:
    username: str
    is_favorite: bool
    label: str


def _absolute_lexical(path: os.PathLike | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _load_config(path: Path) -> TtFavConfig:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    return TtFavConfig(
        api_url=_normalize_api_url(data.get("api_url")),
    )


def _normalize_api_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "api_url is missing; reinstall the tools with --api-url http://SERVER:PORT"
        )

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
        raise ValueError("api_url must be an HTTP(S) URL without credentials, query, or fragment")
    return normalized


def _valid_username(value: str) -> bool:
    return bool(_TIKTOK_USERNAME_PATTERN.fullmatch(value)) and not value.endswith(".")


def determine_action(
    cwd: os.PathLike | str,
    requested_action: Optional[str] = None,
) -> Optional[TtFavAction]:
    cwd_path = _absolute_lexical(cwd)
    username = cwd_path.name
    if not _valid_username(username):
        return None

    is_favorite = requested_action != "del"
    label = "enabled" if is_favorite else "disabled"
    return TtFavAction(username, is_favorite, label)


def _api_request(config: TtFavConfig, method: str, path: str, data: Optional[dict] = None) -> dict:
    body = None
    headers = {"Accept": "application/json"}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(
        f"{config.api_url}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
            payload = response.read()
    except HTTPError as exc:
        payload = exc.read()
        try:
            message = json.loads(payload.decode("utf-8")).get("error")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            message = None
        raise RuntimeError(message or f"API returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"cannot reach API at {config.api_url}: {exc}") from exc

    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("API returned an invalid JSON response") from exc
    if not isinstance(result, dict):
        raise RuntimeError("API returned an unexpected response")
    return result


def _print_invalid_directory_message(cwd: os.PathLike | str) -> None:
    print(f"current directory name is not a valid TikTok username: {Path(cwd).name}")


def _confirm_removal(username: str) -> bool:
    try:
        answer = input(
            f"{username} is already a favorite; remove from favorites? [y/N] "
        )
    except (EOFError, OSError):
        return False
    return answer.strip().lower() in {"y", "yes"}


def run_ttfav(
    config: TtFavConfig,
    cwd: os.PathLike | str,
    dry_run: bool = False,
    requested_action: Optional[str] = None,
) -> int:
    action = determine_action(cwd, requested_action=requested_action)
    if action is None:
        _print_invalid_directory_message(cwd)
        return 1

    user_path = f"/api/users/{quote(action.username, safe='')}"
    try:
        user_response = _api_request(config, "GET", user_path)
        user = user_response.get("user")
        if not isinstance(user, dict) or user.get("username") != action.username:
            raise RuntimeError("API returned an unexpected user response")

        current = bool(user.get("is_favorite"))
        if requested_action is None and current:
            print(f"favorite already enabled: {action.username}")
            if dry_run:
                print(f"would ask before removing favorite: {action.username}")
                return 0
            if not _confirm_removal(action.username):
                print(f"favorite unchanged: {action.username}")
                return 0
            action = TtFavAction(action.username, False, "disabled")

        if dry_run:
            verb = "enable" if action.is_favorite else "disable"
            print(f"would {verb} favorite: {action.username}")
            return 0

        _api_request(
            config,
            "PUT",
            f"{user_path}/favorite",
            {"is_favorite": action.is_favorite},
        )
        if current == action.is_favorite:
            print(f"favorite already {action.label}: {action.username}")
        else:
            print(f"favorite {action.label}: {action.username}")
        return 0
    except RuntimeError as exc:
        print(f"ttfav failed for {action.username}: {exc}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the current-directory TikTok user favorite through the web API."
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=("add", "del"),
        help=(
            "Add or remove the current-directory user; without an action, add"
        ),
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to installed ttfav config JSON")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check the server state without asking or changing it",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config_path = Path(args.config).expanduser()

    if not config_path.exists():
        print(f"ttfav config not found: {config_path}")
        print("Run the TkLiveTracker tools installer first.")
        return 2

    try:
        config = _load_config(config_path)
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"invalid ttfav config: {exc}")
        return 2
    cwd = Path(os.environ.get("PWD", os.getcwd()))
    return run_ttfav(
        config,
        cwd,
        dry_run=args.dry_run,
        requested_action=args.action,
    )


if __name__ == "__main__":
    raise SystemExit(main())
