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
class TtDelConfig:
    api_url: str


class ApiError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _absolute_lexical(path: os.PathLike | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


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
        raise ValueError(
            "api_url must be an HTTP(S) URL without credentials, query, or fragment"
        )
    return normalized


def _load_config(path: Path) -> TtDelConfig:
    with path.open("r", encoding="utf-8") as config_file:
        data = json.load(config_file)
    return TtDelConfig(api_url=_normalize_api_url(data.get("api_url")))


def determine_username(cwd: os.PathLike | str) -> Optional[str]:
    username = _absolute_lexical(cwd).name
    if (
        not _TIKTOK_USERNAME_PATTERN.fullmatch(username)
        or username.endswith(".")
    ):
        return None
    return username


def _api_request(
    config: TtDelConfig,
    method: str,
    path: str,
    data: Optional[dict] = None,
) -> dict:
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
        raise ApiError(message or f"API returned HTTP {exc.code}", exc.code) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ApiError(f"cannot reach API at {config.api_url}: {exc}") from exc

    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("API returned an invalid JSON response") from exc
    if not isinstance(result, dict):
        raise ApiError("API returned an unexpected response")
    return result


def _confirm_deactivation(username: str) -> bool:
    try:
        answer = input(f"deactivate user {username}? [y/N] ")
    except (EOFError, OSError):
        return False
    return answer.strip().lower() in {"y", "yes"}


def run_ttdel(
    config: TtDelConfig,
    cwd: os.PathLike | str,
    dry_run: bool = False,
) -> int:
    username = determine_username(cwd)
    if username is None:
        print(f"current directory name is not a valid TikTok username: {Path(cwd).name}")
        return 1

    user_path = f"/api/users/{quote(username, safe='')}"
    try:
        response = _api_request(config, "GET", user_path)
        user = response.get("user")
        if not isinstance(user, dict) or user.get("username") != username:
            raise ApiError("API returned an unexpected user response")

        if user.get("is_deleted"):
            print(f"user is marked as deleted: {username}")
            return 0
        if not bool(user.get("is_active")):
            print(f"user already inactive: {username}")
            return 0

        if dry_run:
            print(f"would ask before deactivating user: {username}")
            return 0
        if not _confirm_deactivation(username):
            print(f"user remains active: {username}")
            return 0

        result = _api_request(config, "POST", f"{user_path}/deactivate", {})
        if result.get("already_inactive"):
            print(f"user already inactive: {username}")
        else:
            print(f"deactivated user: {username}")
        return 0
    except ApiError as exc:
        if exc.status == 404:
            print(f"user not found in server database: {username}")
        else:
            print(f"ttdel failed for {username}: {exc}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deactivate the current-directory TikTok user through the web API."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to installed tools config JSON",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check the server state without asking or changing it",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = Path(args.config).expanduser()

    if not config_path.exists():
        print(f"tools config not found: {config_path}")
        print("Run the TkLiveTracker tools installer first.")
        return 2

    try:
        config = _load_config(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"invalid ttdel config: {exc}")
        return 2
    cwd = Path(os.environ.get("PWD", os.getcwd()))
    return run_ttdel(config, cwd, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
