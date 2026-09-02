import os
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import yaml

from scripts import doctor as doctor_module
from recorder.utils.utils import read_telegram_config


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_application_has_no_dotenv_dependency() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "python-dotenv" not in pyproject
    assert "default-groups = []" in pyproject
    assert not (PROJECT_ROOT / ".env.example").exists()
    maintained = [
        PROJECT_ROOT / "selenium_supervisor.py",
        PROJECT_ROOT / "scripts" / "selenium_live_monitor.py",
        PROJECT_ROOT / "scripts" / "db_helper.py",
        PROJECT_ROOT / "web_monitor" / "app.py",
    ]
    assert all("load_dotenv" not in path.read_text(encoding="utf-8") for path in maintained)


def test_recorder_upload_secrets_load_from_active_config(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "telegram": {
                "upload": {
                    "api_id": "12345",
                    "api_hash": "test-hash",
                    "bot_token": "test-token",
                    "chat_id": "test-chat",
                    "session_path": "./private",
                }
            }
        }),
        encoding="utf-8",
    )

    loaded = read_telegram_config(config_path)

    assert loaded["api_hash"] == "test-hash"
    assert loaded["session_path"] == str(tmp_path / "private")


def test_doctor_accepts_external_config_and_creates_runtime_dirs(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "deployment"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "paths": {"recordings_path": "./media", "log_path": "./logs"},
            "database": {"path": "./data/main.sqlite"},
            "selenium": {"chrome_profile_path": "./private/chrome"},
            "cookies": {"cookie_json_file": "./private/cookies.json"},
            "web_monitor": {"host": "127.0.0.1", "port": 5001},
        }),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    monkeypatch.setattr(
        doctor_module,
        "_find_chrome",
        lambda _config: "/usr/bin/chromium",
    )
    monkeypatch.setattr(doctor_module, "REQUIRED_IMPORTS", ())

    assert doctor_module.run(str(config_path)) == 0
    assert (config_dir / "media").is_dir()
    assert (config_dir / "logs").is_dir()
    assert (config_dir / "data").is_dir()
    assert stat.S_IMODE((config_dir / "private").stat().st_mode) == 0o700


def test_doctor_uses_configured_chrome_outside_path(monkeypatch, tmp_path) -> None:
    chrome = tmp_path / "custom" / "chrome"
    chrome.parent.mkdir()
    chrome.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    chrome.chmod(0o755)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "paths": {
                "recordings_path": "./recordings",
                "log_path": "./logs",
            },
            "database": {"path": "./data/main.sqlite"},
            "selenium": {"chrome_binary_path": "./custom/chrome"},
            "web_monitor": {"host": "127.0.0.1", "port": 5001},
        }),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    monkeypatch.setattr(doctor_module, "REQUIRED_IMPORTS", ())
    monkeypatch.setattr(doctor_module.shutil, "which", lambda _name: None)

    assert doctor_module.run(str(config_path)) == 0


def test_telegram_upload_dependencies_are_optional() -> None:
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]

    assert not {"pyrogram", "tgcrypto"} & {
        requirement.split(">=", 1)[0] for requirement in project["dependencies"]
    }
    extra = project["optional-dependencies"]["telegram-upload"]
    assert {requirement.split(">=", 1)[0] for requirement in extra} == {
        "pyrogram",
        "tgcrypto",
    }


def test_setup_is_idempotent_and_does_not_overwrite_config(tmp_path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(PROJECT_ROOT / "scripts" / "setup.sh", scripts / "setup.sh")
    shutil.copy2(PROJECT_ROOT / "config.example.yaml", repo / "config.example.yaml")
    for command in ("uv",):
        executable = fake_bin / command
        executable.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    subprocess.run([scripts / "setup.sh"], cwd=repo, env=environment, check=True)
    config = repo / "config.yaml"
    config.write_text("sentinel: preserved\n", encoding="utf-8")
    subprocess.run([scripts / "setup.sh"], cwd=repo, env=environment, check=True)

    assert config.read_text(encoding="utf-8") == "sentinel: preserved\n"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE((repo / "private").stat().st_mode) == 0o700
