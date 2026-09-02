import base64
import sys
from pathlib import Path

from scripts import capture_public_screenshots as capture


class FakeCaptureDriver:
    def __init__(self) -> None:
        self.script_calls: list[tuple[object, ...]] = []

    def execute_script(self, script: str, *args: object) -> None:
        self.script_calls.append((script, *args))

    def execute_cdp_cmd(self, command: str, params: dict[str, object]) -> dict[str, str]:
        assert command == "Page.captureScreenshot"
        assert params["format"] == "png"
        return {"data": base64.b64encode(b"public-png").decode("ascii")}


def test_scrollbars_are_hidden_by_default_and_can_be_shown(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["capture_public_screenshots.py"])
    assert capture.parse_args().show_scrollbars is False

    monkeypatch.setattr(sys, "argv", ["capture_public_screenshots.py", "--show-scrollbars"])
    assert capture.parse_args().show_scrollbars is True


def test_capture_png_configures_scrollbar_visibility(tmp_path: Path) -> None:
    hidden_driver = FakeCaptureDriver()
    capture.capture_png(
        hidden_driver,
        tmp_path / "hidden.png",
        hide_scrollbars=True,
    )

    assert hidden_driver.script_calls[0][-1] is True
    assert "scrollbar-width: none" in capture.CAPTURE_SCROLLBAR_CSS
    assert "::-webkit-scrollbar" in capture.CAPTURE_SCROLLBAR_CSS

    visible_driver = FakeCaptureDriver()
    capture.capture_png(
        visible_driver,
        tmp_path / "visible.png",
        hide_scrollbars=False,
    )
    assert visible_driver.script_calls[0][-1] is False


def test_recipe_actions_and_public_screenshot_management_remain_supported(
    tmp_path: Path,
    capsys,
) -> None:
    recipes_path = tmp_path / "public_screenshots.yaml"
    recipes_path.write_text(
        """version: 1
captures:
  - name: users-filtered
    page: users
    theme: dark
    filename: 01-users-filtered.png
    actions:
      - type: select
        locator:
          by: id
          value: live-filter
        value: live
""",
        encoding="utf-8",
    )

    document = capture.load_recipes(recipes_path)
    assert document["captures"][0]["actions"][0]["value"] == "live"

    capture.list_recipes(document, recipes_path)
    assert "01-users-filtered.png" in capsys.readouterr().out
    assert capture.remove_recipe(document, "users-filtered") is True
    assert document["captures"] == []

    reviewed_dir = tmp_path / "reviewed"
    public_dir = tmp_path / "public"
    reviewed_dir.mkdir()
    public_dir.mkdir()
    png_bytes = b"\x89PNG\r\n\x1a\nimage"
    (reviewed_dir / "01-users-filtered.png").write_bytes(png_bytes)
    (public_dir / "stale.png").write_bytes(png_bytes)
    published = {
        "version": 1,
        "captures": [
            {
                "name": "users-filtered",
                "page": "users",
                "theme": "dark",
                "filename": "01-users-filtered.png",
                "actions": [],
            }
        ],
    }

    added, updated, deleted = capture.sync_public_screenshots(
        reviewed_dir,
        public_dir,
        published,
    )
    assert added == ["01-users-filtered.png"]
    assert updated == []
    assert deleted == ["stale.png"]
