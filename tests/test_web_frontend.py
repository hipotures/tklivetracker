from pathlib import Path


def test_edit_user_fetches_exact_user_endpoint() -> None:
    project_root = Path(__file__).resolve().parent.parent

    for relative_path in ["web_monitor/static/js/users.js"]:
        source = (project_root / relative_path).read_text(encoding="utf-8")
        assert "async fetchUserForEdit(username)" in source or (
            "TikTokRecorderApp.prototype.fetchUserForEdit = async function(username)" in source
        )
        assert "search: username" not in source
        assert "per_page: 1" not in source
        assert "apiRequest(`/api/users/${encodeURIComponent(username)}`)" in source


def test_incomplete_web_push_and_service_worker_assets_are_removed() -> None:
    project_root = Path(__file__).resolve().parent.parent
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (project_root / "web_monitor").rglob("*.js")
    )

    assert "PushManager" not in sources
    assert "pushManager" not in sources
    assert "getVAPIDPublicKey" not in sources
    assert "serviceWorker.register" not in sources
    assert not (project_root / "web_monitor" / "sw.js").exists()
    assert not (project_root / "web_monitor" / "static" / "sw.js").exists()
    assert not (project_root / "web_monitor" / "static" / "js" / "app.js").exists()


def test_dynamic_api_and_sse_routes_have_no_service_worker_cache() -> None:
    from web_monitor.app import create_app

    app = create_app(read_only=True)
    routes = {rule.rule for rule in app.url_map.iter_rules()}
    assert {"/api/users", "/api/supervisor-details", "/api/events"} <= routes
    assert "/sw.js" not in routes


def test_manifest_referenced_local_assets_exist() -> None:
    import json
    import xml.etree.ElementTree as ET

    project_root = Path(__file__).resolve().parent.parent
    manifest = json.loads(
        (project_root / "web_monitor" / "static" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    for icon in manifest.get("icons", []):
        source = icon["src"]
        assert source.startswith("/static/")
        assert (project_root / "web_monitor" / "static" / source.removeprefix("/static/")).is_file()

    assert (project_root / "web_monitor" / "static" / "vendor" / "chartjs" / "chart.umd.min.js").is_file()

    lucide_dir = project_root / "web_monitor" / "static" / "vendor" / "lucide"
    sprite_path = lucide_dir / "icons.svg"
    assert sprite_path.is_file()
    assert (lucide_dir / "LICENSE").is_file()
    symbol_ids = {
        element.attrib["id"]
        for element in ET.parse(sprite_path).getroot()
        if element.tag.endswith("symbol")
    }
    assert {
        "bell", "bell-off", "circle", "circle-check", "circle-x", "clock",
        "info", "lock-keyhole", "megaphone", "moon", "refresh-cw", "settings",
        "star", "sun", "triangle-alert", "x",
    } <= symbol_ids


def test_public_ui_uses_local_lucide_icons_instead_of_color_emoji() -> None:
    project_root = Path(__file__).resolve().parent.parent
    template_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (project_root / "web_monitor" / "templates").rglob("*.html")
    )
    dynamic_sources = "\n".join(
        (project_root / "web_monitor" / "static" / "js" / name).read_text(encoding="utf-8")
        for name in ["core.js", "users.js", "stats.js"]
    )
    dashboard_source = (
        project_root / "web_monitor" / "static" / "js" / "dashboard.js"
    ).read_text(encoding="utf-8")

    assert "vendor/lucide/icons.svg" in template_sources
    for emoji in ["🔴", "⚫", "✅", "❌", "⭐", "🔔", "🔕", "🔒", "⏰", "☀️", "🌙", "⚙️", "📢"]:
        assert emoji not in template_sources
        assert emoji not in dynamic_sources
    assert "`🔴 ${username} is live!`" not in dashboard_source
    assert "`⏰ ${durationText}`" not in dashboard_source
    assert "user.notifications_enabled ? '🔔' : '🔕'" not in dashboard_source


def test_dashboard_handles_room_id_unavailable_alert() -> None:
    project_root = Path(__file__).resolve().parent.parent
    source = (
        project_root / "web_monitor" / "static" / "js" / "dashboard.js"
    ).read_text(encoding="utf-8")

    assert "case 'recorder_room_id_unavailable':" in source
    assert "'Recording did not start'" in source


def test_live_and_favorite_cards_leave_user_editing_to_users_page() -> None:
    project_root = Path(__file__).resolve().parent.parent
    dashboard_source = (
        project_root / "web_monitor" / "static" / "js" / "dashboard.js"
    ).read_text(encoding="utf-8")
    users_source = (
        project_root / "web_monitor" / "static" / "js" / "users.js"
    ).read_text(encoding="utf-8")

    assert 'class="stream-actions"' not in dashboard_source
    assert 'class="favorite-actions"' not in dashboard_source
    assert 'data-user-action="edit"' in users_source


def test_dashboard_live_cards_are_compact_without_reducing_live_now_details() -> None:
    project_root = Path(__file__).resolve().parent.parent
    source = (
        project_root / "web_monitor" / "static" / "js" / "dashboard.js"
    ).read_text(encoding="utf-8")
    dashboard_renderer = source.split(
        "TikTokRecorderApp.prototype.updateLiveUsers", 1
    )[1].split("TikTokRecorderApp.prototype.loadLivePage", 1)[0]
    live_now_renderer = source.split(
        "TikTokRecorderApp.prototype.updateLivePageContent", 1
    )[1].split("TikTokRecorderApp.prototype.loadFavoritesPage", 1)[0]

    assert "<strong>Duration:</strong>" in dashboard_renderer
    assert "<strong>Started:</strong>" in dashboard_renderer
    assert "<strong>Previous:</strong>" not in dashboard_renderer
    assert "<strong>Total Lives:</strong>" not in dashboard_renderer
    assert '<span class="live-pill">LIVE</span>' not in dashboard_renderer

    assert "<strong>Duration:</strong>" in live_now_renderer
    assert "<strong>Current Live Started:</strong>" in live_now_renderer
    assert "<strong>Previous Live:</strong>" in live_now_renderer
    assert "<strong>Check Interval:</strong>" in live_now_renderer
    assert "this.legacySchedulerStatsEnabled === true" in live_now_renderer
    assert "${checkIntervalInfo}" in live_now_renderer
    assert "<strong>Total Lives:</strong>" in live_now_renderer
    assert '<span class="live-pill">LIVE</span>' not in live_now_renderer
    assert 'class="stream-header-icons"' in live_now_renderer
    assert "${recordingStatus}" in live_now_renderer.split(
        'class="stream-header-icons"', 1
    )[1].split("</div>", 1)[0]


def test_dashboard_live_grid_uses_four_columns_only_on_wide_screens() -> None:
    project_root = Path(__file__).resolve().parent.parent
    css = (
        project_root / "web_monitor" / "static" / "css" / "style.css"
    ).read_text(encoding="utf-8")

    assert "@media (min-width: 1700px)" in css
    assert (
        ".live-users-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }"
        in css
    )


def test_quick_add_and_stats_use_public_validation_and_browser_locale() -> None:
    project_root = Path(__file__).resolve().parent.parent
    users_source = (
        project_root / "web_monitor" / "static" / "js" / "users.js"
    ).read_text(encoding="utf-8")
    stats_source = (
        project_root / "web_monitor" / "static" / "js" / "stats.js"
    ).read_text(encoding="utf-8")

    assert "/^[A-Za-z0-9._]{2,24}$/" in users_source
    assert "const username = input.value;" in users_source
    assert "normalizedUsername.endsWith('.')" in users_source
    assert "255 characters" not in users_source
    assert "toLocaleString('pl-PL'" not in stats_source
    assert "toLocaleString(undefined" in stats_source


def test_public_branding_uses_tk_live_tracker_name_and_tlt_mark() -> None:
    project_root = Path(__file__).resolve().parent.parent
    layout = (
        project_root / "web_monitor" / "templates" / "layout.html"
    ).read_text(encoding="utf-8")
    manifest = (
        project_root / "web_monitor" / "static" / "manifest.json"
    ).read_text(encoding="utf-8")

    assert '<span class="logo" aria-hidden="true">TLT</span>' in layout
    assert '<span class="app-title-text">TkLiveTracker</span>' in layout
    assert '"name": "TkLiveTracker"' in manifest


def test_database_statistics_load_only_from_manual_admin_refresh() -> None:
    project_root = Path(__file__).resolve().parent.parent
    core_source = (
        project_root / "web_monitor" / "static" / "js" / "core.js"
    ).read_text(encoding="utf-8")
    dashboard_source = (
        project_root / "web_monitor" / "static" / "js" / "dashboard.js"
    ).read_text(encoding="utf-8")

    refresh_handler = core_source.split(
        "const adminRefreshBtn = document.getElementById('admin-refresh');", 1
    )[1].split("const connectionRetry", 1)[0]
    auto_refresh = dashboard_source.split(
        "TikTokRecorderApp.prototype.startAutoRefresh", 1
    )[1]

    assert "this.loadDatabaseStatistics()" in refresh_handler
    assert "this.loadDatabaseStatistics()" not in auto_refresh
    assert "apiRequest('/api/database-statistics')" in dashboard_source


def test_mobile_navigation_auto_hides_without_affecting_desktop() -> None:
    project_root = Path(__file__).resolve().parent.parent
    core_source = (
        project_root / "web_monitor" / "static" / "js" / "core.js"
    ).read_text(encoding="utf-8")
    css_source = (
        project_root / "web_monitor" / "static" / "css" / "style.css"
    ).read_text(encoding="utf-8")

    assert "setupMobileChromeAutoHide()" in core_source
    assert "{ passive: true }" in core_source
    assert "prefers-reduced-motion: reduce" in core_source
    mobile_media_start = css_source.index("@media (max-width: 900px)")
    hidden_rule = css_source.index("body.mobile-chrome-hidden .header")
    assert hidden_rule > mobile_media_start
