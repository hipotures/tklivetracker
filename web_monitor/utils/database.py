import sqlite3
from flask import g, current_app

from utils.config_paths import resolve_path

def get_db():
    """Get database connection with row factory"""
    db = getattr(g, '_database', None)
    if db is None:
        try:
            db_path = resolve_path(current_app.config['DATABASE'])
            db = g._database = sqlite3.connect(
                db_path,
                timeout=30.0,  # Wait up to 30 seconds for locks
                check_same_thread=False,  # Allow Flask to use connection across threads
                isolation_level=None  # Autocommit mode to prevent long transactions
            )
            db.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrency
            db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as e:
            current_app.logger.error(f"Database connection error: {e}")
            raise
    return db

def close_connection(exception):
    """Close database connection"""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()
