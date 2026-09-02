import sys
import logging
import argparse
from urllib.parse import urlsplit
from pathlib import Path
from flask import Flask, jsonify, request
from flask.logging import default_handler

# Add project root to path so package imports work when running as a script
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

# Import utilities
from web_monitor.utils.config import load_config
from web_monitor.utils.database import close_connection

# Import blueprints
from web_monitor.blueprints.main import main_bp
from web_monitor.blueprints.api import api_bp
from web_monitor.blueprints.stats import stats_bp
from web_monitor.blueprints.sse import sse_bp
from web_monitor.blueprints.analytics import analytics_bp


def normalize_allowed_origin(value):
    """Return a canonical HTTP(S) origin or raise for invalid configuration."""
    if not isinstance(value, str):
        raise ValueError("web_monitor.allowed_origins entries must be strings")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"Invalid web_monitor.allowed_origins entry: {value!r}")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"

def create_app(read_only=False, config_path=None):
    """Application factory function"""
    app = Flask(__name__)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    # Load configuration
    config, DATABASE_PATH, RECORDINGS_PATH, RECORDINGS_FAV_PATH, FAVORITE_SOURCE_PATH = load_config(
        config_path
    )
    app.config['DATABASE'] = DATABASE_PATH
    app.config['RECORDINGS_PATH'] = RECORDINGS_PATH
    app.config['RECORDINGS_FAV_PATH'] = RECORDINGS_FAV_PATH
    app.config['FAVORITE_SOURCE_PATH'] = FAVORITE_SOURCE_PATH
    app.config['CONFIG'] = config
    app.config['READ_ONLY'] = read_only
    configured_origins = config.get("web_monitor", {}).get("allowed_origins", [])
    if not isinstance(configured_origins, list):
        raise ValueError("web_monitor.allowed_origins must be a list")
    app.config['ALLOWED_ORIGINS'] = {
        normalize_allowed_origin(origin) for origin in configured_origins
    }

    # Setup logging
    if not app.debug:
        if default_handler in app.logger.handlers:
            app.logger.removeHandler(default_handler)

        has_web_handler = any(
            getattr(handler, "_ttracker_web_monitor_handler", False)
            for handler in app.logger.handlers
        )
        if not has_web_handler:
            log_handler = logging.StreamHandler(sys.stdout)
            log_handler.setLevel(logging.INFO)
            log_handler._ttracker_web_monitor_handler = True
            app.logger.addHandler(log_handler)

        app.logger.setLevel(logging.INFO)

    # Register teardown handler for database connections
    app.teardown_appcontext(close_connection)

    # Register blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(stats_bp)
    app.register_blueprint(sse_bp)
    app.register_blueprint(analytics_bp)

    @app.before_request
    def reject_cross_origin_mutations():
        """Block browser cross-origin writes while allowing non-browser clients."""
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None

        origin = request.headers.get("Origin")
        if not origin:
            return None

        try:
            browser_origin = normalize_allowed_origin(origin)
        except ValueError:
            browser_origin = None
        direct_origin = normalize_allowed_origin(f"{request.scheme}://{request.host}")
        accepted_origins = {direct_origin, *app.config['ALLOWED_ORIGINS']}
        if browser_origin not in accepted_origins:
            return jsonify({"error": "Cross-origin write request rejected"}), 403
        return None

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    return app


def resolve_server_address(config, host_override=None, port_override=None):
    """Resolve the web bind address with CLI values taking precedence."""
    web_config = config.get("web_monitor", {})
    host = host_override if host_override is not None else web_config.get("host", "0.0.0.0")
    port = port_override if port_override is not None else web_config.get("port", 5001)

    if not isinstance(host, str) or not host.strip():
        raise ValueError("web_monitor.host must be a non-empty string")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("web_monitor.port must be an integer") from None
    if not 1 <= port <= 65535:
        raise ValueError("web_monitor.port must be between 1 and 65535")
    return host.strip(), port

def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description='TkLiveTracker Web Monitor')
    parser.add_argument('--read-only', action='store_true',
                       help='Run in read-only mode (no write operations allowed)')
    parser.add_argument('--config', default=None,
                       help='Path to configuration file (default: project config.yaml)')
    parser.add_argument('--host', default=None,
                       help='Override web_monitor.host from config.yaml')
    parser.add_argument('--port', type=int, default=None,
                       help='Override web_monitor.port from config.yaml')
    return parser.parse_args()

if __name__ == '__main__':
    args = parse_arguments()
    app = create_app(read_only=args.read_only, config_path=args.config)
    try:
        host, port = resolve_server_address(
            app.config['CONFIG'], args.host, args.port
        )
    except ValueError as error:
        raise SystemExit(f"Configuration error: {error}") from error

    if args.read_only:
        print(f"🔒 Starting web monitor in READ-ONLY mode on {host}:{port}")
        if host not in {"127.0.0.1", "::1", "localhost"}:
            print(
                "WARNING: The dashboard data is remotely visible. Read-only "
                "mode is not authentication; provide firewall, transport, and "
                "access controls externally."
            )
    else:
        print(f"🌐 Starting web monitor on {host}:{port}")

    app.run(debug=False, host=host, port=port)
