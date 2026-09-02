import io

from scripts.selenium_live_monitor import SessionManager, print_inverse_terminal_banner


class VisibleElement:
    def is_displayed(self) -> bool:
        return True


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class AuthStateDriver:
    def __init__(
        self,
        *,
        current_url: str,
        visible_xpaths: set[str] | None = None,
        cookies: list[dict[str, str]] | None = None,
    ):
        self.current_url = current_url
        self.visible_xpaths = visible_xpaths or set()
        self.cookies = cookies or []
        self.requested_urls: list[str] = []

    def get(self, url: str) -> None:
        self.requested_urls.append(url)

    def find_elements(self, by, xpath: str):
        return [VisibleElement()] if xpath in self.visible_xpaths else []

    def get_cookies(self):
        return self.cookies


def test_startup_login_check_confirms_authenticated_profile(monkeypatch) -> None:
    monkeypatch.setattr("scripts.selenium_live_monitor.time.sleep", lambda _seconds: None)
    driver = AuthStateDriver(
        current_url="https://www.tiktok.com/live",
        visible_xpaths={"//button[@data-e2e='top-profile-avatar']"},
    )

    status = SessionManager(driver, "unused.json").check_login_status()

    assert status is True
    assert driver.requested_urls == ["https://www.tiktok.com/live"]


def test_startup_login_check_detects_visible_login_prompt(monkeypatch) -> None:
    monkeypatch.setattr("scripts.selenium_live_monitor.time.sleep", lambda _seconds: None)
    driver = AuthStateDriver(
        current_url="https://www.tiktok.com/live",
        visible_xpaths={"//button[normalize-space(.)='Log in']"},
    )

    assert SessionManager(driver, "unused.json").check_login_status() is False


def test_startup_login_check_confirms_authenticated_session_cookie(monkeypatch) -> None:
    monkeypatch.setattr("scripts.selenium_live_monitor.time.sleep", lambda _seconds: None)
    driver = AuthStateDriver(
        current_url="https://www.tiktok.com/live",
        cookies=[{"name": "sessionid", "value": "test-placeholder"}],
    )

    assert SessionManager(driver, "unused.json").check_login_status() is True


def test_visible_login_prompt_wins_over_stale_session_cookie(monkeypatch) -> None:
    monkeypatch.setattr("scripts.selenium_live_monitor.time.sleep", lambda _seconds: None)
    driver = AuthStateDriver(
        current_url="https://www.tiktok.com/live",
        visible_xpaths={"//button[normalize-space(.)='Log in']"},
        cookies=[{"name": "sessionid", "value": "expired-test-placeholder"}],
    )

    assert SessionManager(driver, "unused.json").check_login_status() is False


def test_startup_login_check_detects_login_redirect(monkeypatch) -> None:
    monkeypatch.setattr("scripts.selenium_live_monitor.time.sleep", lambda _seconds: None)
    driver = AuthStateDriver(current_url="https://www.tiktok.com/login?redirect_url=/live")

    assert SessionManager(driver, "unused.json").check_login_status() is False


def test_startup_login_check_does_not_treat_missing_following_section_as_logout(
    monkeypatch,
) -> None:
    monkeypatch.setattr("scripts.selenium_live_monitor.time.sleep", lambda _seconds: None)
    driver = AuthStateDriver(current_url="https://www.tiktok.com/live")

    assert SessionManager(driver, "unused.json").check_login_status() is None


def test_logged_out_banner_uses_terminal_inversion() -> None:
    output = TtyBuffer()

    print_inverse_terminal_banner(["TIKTOK SESSION IS LOGGED OUT"], stream=output)

    rendered = output.getvalue()
    assert "\033[7m" in rendered
    assert "\033[0m" in rendered
    assert "TIKTOK SESSION IS LOGGED OUT" in rendered
