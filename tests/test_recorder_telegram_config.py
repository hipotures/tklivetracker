from pathlib import Path

from recorder.upload import telegram as telegram_module


def test_recorder_telegram_session_stays_in_private_directory(monkeypatch, tmp_path):
    captured = {}

    class FakeClient:
        def __init__(self, name, **kwargs):
            captured.update(name=name, **kwargs)

    class FakeParseMode:
        HTML = "html"

    monkeypatch.setattr(
        telegram_module,
        "read_telegram_config",
        lambda config_path: {
            "api_id": 12345,
            "api_hash": "representative-hash",
            "bot_token": "representative-token",
            "chat_id": "representative-chat",
            "session_path": str(tmp_path / "private"),
        },
    )
    monkeypatch.setattr(
        telegram_module,
        "_load_pyrogram",
        lambda: (FakeClient, FakeParseMode),
    )
    telegram_module.Telegram(tmp_path / "config.yaml")

    workdir = Path(captured["workdir"])
    assert workdir.name == "private"
    assert workdir.is_dir()
    assert captured["name"] == "telegram_session"

    session_file = workdir / "telegram_session.session"
    session_file.write_text("fixture", encoding="utf-8")
    session_file.chmod(0o644)
    uploader = telegram_module.Telegram(tmp_path / "config.yaml")
    uploader._protect_session_files()
    assert session_file.stat().st_mode & 0o077 == 0
