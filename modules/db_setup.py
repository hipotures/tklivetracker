import sqlite3
import logging

from utils.config_paths import resolve_path

logger = logging.getLogger(__name__)

def create_connection(db_path):
    """Create database connection with timeout and WAL mode for better concurrency"""
    resolved_db_path = resolve_path(db_path)
    conn = sqlite3.connect(
        resolved_db_path,
        timeout=30.0,
        check_same_thread=False,
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
    )
    conn.row_factory = sqlite3.Row # Allows access to columns by name
    # Enable WAL mode for better concurrency
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def initialize_db(conn):
    cursor = conn.cursor()

    # Note: SQLite doesn't have a native BOOLEAN type. BOOLEAN columns are stored as INTEGER (0 or 1).
    # In queries, use numeric values: WHERE is_active = 1 (not WHERE is_active = TRUE)
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            priority INTEGER DEFAULT 10,
            next_check TIMESTAMP,
            check_interval INTEGER DEFAULT 300,
            is_live BOOLEAN DEFAULT FALSE,
            total_lives INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT (DATETIME('now', 'localtime')),
            is_active BOOLEAN DEFAULT TRUE,
            is_favorite BOOLEAN DEFAULT FALSE,
            notifications_enabled BOOLEAN DEFAULT FALSE,
            tt_user_id TEXT,
            tt_secuid TEXT,
            last_deactivated_at TIMESTAMP,
            deleted_at TIMESTAMP
        )
        '''
    )
    # Check and potentially add added_at column to existing table
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN added_at TIMESTAMP DEFAULT (DATETIME('now', 'localtime'))")
        logger.info("Added 'added_at' column to existing users table.")
    except sqlite3.OperationalError:
        # Column already exists, ignore error
        pass

    # Check and potentially add is_active column to existing table
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT TRUE")
        logger.info("Added 'is_active' column to existing users table.")
    except sqlite3.OperationalError:
        # Column already exists, ignore error
        pass

    # Check and potentially add is_favorite column to existing table
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_favorite BOOLEAN DEFAULT FALSE")
        logger.info("Added 'is_favorite' column to existing users table.")
    except sqlite3.OperationalError:
        # Column already exists, ignore error
        pass

    # Check and potentially add notifications_enabled column to existing table
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN notifications_enabled BOOLEAN DEFAULT FALSE")
        logger.info("Added 'notifications_enabled' column to existing users table.")
    except sqlite3.OperationalError:
        # Column already exists, ignore error
        pass

    # Check and potentially add tt_user_id column to existing table
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN tt_user_id TEXT")
        logger.info("Added 'tt_user_id' column to existing users table.")
    except sqlite3.OperationalError:
        # Column already exists, ignore error
        pass

    # Check and potentially add tt_secuid column to existing table
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN tt_secuid TEXT")
        logger.info("Added 'tt_secuid' column to existing users table.")
    except sqlite3.OperationalError:
        # Column already exists, ignore error
        pass

    # Check and potentially add last_deactivated_at column to existing table
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN last_deactivated_at TIMESTAMP")
        logger.info("Added 'last_deactivated_at' column to existing users table.")
    except sqlite3.OperationalError:
        # Column already exists, ignore error
        pass

    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS lives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            tiktok_stream_id TEXT,
            tiktok_started_at TIMESTAMP,
            tiktok_owner_user_id TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        '''
    )

    lives_columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(lives)").fetchall()
    }
    if 'tiktok_stream_id' not in lives_columns:
        cursor.execute("ALTER TABLE lives ADD COLUMN tiktok_stream_id TEXT")
        logger.info("Added 'tiktok_stream_id' column to existing lives table.")
    if 'tiktok_started_at' not in lives_columns:
        cursor.execute("ALTER TABLE lives ADD COLUMN tiktok_started_at TIMESTAMP")
        logger.info("Added 'tiktok_started_at' column to existing lives table.")
    if 'tiktok_owner_user_id' not in lives_columns:
        cursor.execute("ALTER TABLE lives ADD COLUMN tiktok_owner_user_id TEXT")
        logger.info("Added 'tiktok_owner_user_id' column to existing lives table.")

    # Create live_processes table for persistent live system
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS live_processes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT NOT NULL,
            pid INTEGER NOT NULL,
            recording_file_path TEXT,
            started_at TIMESTAMP DEFAULT (DATETIME('now', 'localtime')),
            last_health_check TIMESTAMP,
            health_status TEXT DEFAULT 'healthy',
            restart_count INTEGER DEFAULT 0,
            file_size_bytes INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE,
            room_id TEXT,
            live_id INTEGER,
            FOREIGN KEY (live_id) REFERENCES lives(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        '''
    )

    # Create supervisor_status table for supervisor monitoring
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS supervisor_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pid INTEGER NOT NULL,
            started_at TIMESTAMP DEFAULT (DATETIME('now', 'localtime')),
            last_heartbeat TIMESTAMP DEFAULT (DATETIME('now', 'localtime')),
            status TEXT DEFAULT 'active',
            version TEXT,
            stats_json TEXT,
            config_hash TEXT
        )
        '''
    )

    # Create indices for faster searching
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_tt_user_id ON users(tt_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_tt_secuid ON users(tt_secuid)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_is_favorite ON users(is_favorite)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_lives_user_id ON lives(user_id)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_lives_tiktok_owner_user_id "
        "ON lives(tiktok_owner_user_id)"
    )

    conn.commit()
