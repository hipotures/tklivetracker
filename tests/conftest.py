import site
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_SITE_PACKAGES = PROJECT_ROOT / ".venv" / "lib" / "python3.12" / "site-packages"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if VENV_SITE_PACKAGES.exists():
    site.addsitedir(str(VENV_SITE_PACKAGES))


@pytest.fixture(autouse=True)
def isolate_web_monitor_configuration(monkeypatch, tmp_path: Path):
    """Prevent web tests from loading a developer or production config.yaml."""
    import web_monitor.app as web_app

    database = tmp_path / "default-web.db"
    recordings = tmp_path / "recordings"
    favorites = tmp_path / "favorites"
    real_load_config = web_app.load_config

    def load_isolated_config(config_path=None):
        if config_path is not None:
            return real_load_config(config_path)
        return (
            {},
            str(database),
            str(recordings),
            str(favorites),
            str(recordings),
        )

    monkeypatch.setattr(
        web_app,
        "load_config",
        load_isolated_config,
    )
