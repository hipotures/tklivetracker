import sys
import os
import json
import argparse
from datetime import datetime
import logging
import yaml

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(project_root)

from modules import utils
from modules.db_setup import create_connection
from modules.db_user import get_user_id, add_live_session, end_live_session, set_is_live
from utils.config_paths import absolute_config_path, normalize_config_paths

config_arg_parser = argparse.ArgumentParser(add_help=False)
config_arg_parser.add_argument(
    "--config",
    default=os.path.join(project_root, "config.yaml"),
)
config_args, _ = config_arg_parser.parse_known_args()
config_path = absolute_config_path(config_args.config)
config = {}
try:
    with open(config_path, "r", encoding="utf-8") as f:
        config = normalize_config_paths(yaml.safe_load(f) or {}, config_path)
except FileNotFoundError:
    print(
        f"Warning: Config file not found at {config_path}, using default log level INFO.",
        file=sys.stderr,
    )
except Exception as e:
    print(
        f"Warning: Error loading config file {config_path}: {e}, using default log level INFO.",
        file=sys.stderr,
    )

log_level_str = config.get("logging", {}).get("log_level", "INFO")

# Get log path from config, with better fallback handling
log_path = config.get("paths", {}).get("log_path")

if not log_path:
    # Fallback only if log_path is not in config.
    log_path = os.path.join(
        os.path.dirname(config_path), "logs", "aplikacja.log"
    )
    print(
        f"Warning: log_path not found in config, using fallback: {log_path}",
        file=sys.stderr,
    )
else:
    # If log_path is a directory, append the log filename from config
    if os.path.isdir(log_path):
        log_filename = config.get('logging', {}).get('supervisor_log_file', 'supervisor.log')
        log_path = os.path.join(log_path, log_filename)
        print(f"log_path was a directory, using: {log_path}", file=sys.stderr)

logger = utils.setup_logger(log_path, logger_name="HLP", log_level_str=log_level_str)

# Configure loggers for db modules
for db_module in ["modules.db_setup", "modules.db_user", "modules.db_users"]:
    db_logger = logging.getLogger(db_module)
    db_logger.setLevel(utils.LOG_LEVELS.get(log_level_str.upper(), logging.INFO))
    if not db_logger.hasHandlers():
        if logger.handlers:
            handler = logger.handlers[0]
            db_logger.addHandler(handler)
        else:
            db_logger.addHandler(logging.StreamHandler())


def get_database_path():
    """Resolve the main SQLite path from config.yaml."""
    return config.get("database", {}).get("path") or os.path.join(
        os.path.dirname(config_path), "db.sqlite"
    )

def main():
    parser = argparse.ArgumentParser(description="Update recorder state in the active database")
    parser.add_argument("--config", default=os.path.join(project_root, "config.yaml"))
    parser.add_argument("action", choices=("start", "stop", "set_is_live", "get_is_live"))
    parser.add_argument("username")
    parser.add_argument("values", nargs="*")
    args = parser.parse_args()

    action = args.action
    username = args.username
    db_path = get_database_path()
    conn = create_connection(db_path)

    if action == "start":
        user_id = get_user_id(conn, username)
        if not user_id:
            print(f"User {username} not found in DB.")
            sys.exit(2)
        tiktok_stream_id = (
            args.values[0] if args.values and args.values[0] else None
        )
        tiktok_started_at = None
        if len(args.values) > 1 and args.values[1]:
            try:
                tiktok_started_at = datetime.fromtimestamp(int(args.values[1]))
            except (TypeError, ValueError, OSError) as error:
                logger.warning(f"Invalid TikTok startTime {args.values[1]!r}: {error}")
        tiktok_owner_user_id = (
            args.values[2] if len(args.values) > 2 and args.values[2] else None
        )

        live_id = add_live_session(
            conn,
            user_id,
            datetime.now(),
            tiktok_stream_id=tiktok_stream_id,
            tiktok_started_at=tiktok_started_at,
            tiktok_owner_user_id=tiktok_owner_user_id,
        )
        print(json.dumps({"live_id": live_id}))
    elif action == "stop":
        if not args.values:
            parser.error("stop requires live_id")
        live_id = int(args.values[0])
        logger.info(f"[DEBUG] db_helper stop called: username={username}, live_id={live_id}")
        # The recorder owns this lives row, but not the user's global live state.
        # The supervisor resets users.is_live only after the last recorder exits.
        end_live_session(conn, live_id, datetime.now(), reset_user_status=False)
        logger.info(f"[DEBUG] db_helper stop completed successfully for live_id={live_id}")
        print("OK")
    elif action == "set_is_live":
        if not args.values:
            parser.error("set_is_live requires value")
        value = int(args.values[0])
        set_is_live(conn, username, value)
        print("OK")
    elif action == "get_is_live":
        cursor = conn.cursor()
        cursor.execute("SELECT is_live FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        if row is not None:
            print(row[0])
        else:
            print("0")
if __name__ == "__main__":
    main()
