import sqlite3
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

def reset_is_live_all(conn):
    cursor = conn.cursor()
    cursor.execute("UPDATE lives SET ended_at = DATETIME('now', 'localtime') WHERE ended_at IS NULL")
    closed_sessions = cursor.rowcount
    sql = "UPDATE users SET is_live = 0"
    logger.debug(f"Executing SQL: {sql}")
    cursor.execute(sql)
    conn.commit()
    logger.debug(f"All is_live flags reset to 0. Closed active sessions: {closed_sessions}")

def get_previous_recording_users(conn):
    """
    Identifies users who were live in the previous run (is_live = 1).
    This function is called at startup before any reset operations.
    """
    cursor = conn.cursor()

    # Get users with is_live = 1
    cursor.execute("""
        SELECT username
        FROM users
        WHERE is_live = 1
        ORDER BY username
    """)
    live_users = cursor.fetchall()

    # Get the supervisor logger directly to ensure the message is logged
    sup_logger = logging.getLogger("SUP")

    if live_users:
        usernames = [row['username'] for row in live_users]
        # Log directly to the supervisor logger
        sup_logger.info(f"Previous recording users: {', '.join(usernames)}")
    else:
        # Log directly to the supervisor logger
        sup_logger.info("No users with is_live=1 found at startup")

    return live_users

def reset_stuck_live_users_and_prioritize_check(conn):
    """
    Finds users potentially stuck with is_live=1 (e.g., after a crash)
    and resets their status while prioritizing them for the next check.
    Returns the number of users updated.
    """
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE lives
        SET ended_at = DATETIME('now', 'localtime')
        WHERE ended_at IS NULL
          AND user_id IN (SELECT id FROM users WHERE is_live = 1)
    """)
    closed_sessions = cursor.rowcount

    # Set next_check to NULL to ensure highest priority in sorting
    sql = "UPDATE users SET is_live = 0, next_check = NULL WHERE is_live = 1"
    logger.debug(f"Executing SQL: {sql} (Resetting stuck live users and prioritizing check by setting next_check=NULL)")
    cursor.execute(sql)
    updated_count = cursor.rowcount
    conn.commit()
    if updated_count > 0:
        logger.info(
            f"Reset is_live=0 and prioritized next_check for {updated_count} potentially stuck user(s). "
            f"Closed sessions: {closed_sessions}."
        )
    else:
        logger.debug(f"No stuck live users found to reset. Closed sessions: {closed_sessions}.")

    return updated_count

def get_all_usernames(conn):
    cursor = conn.cursor()
    sql = "SELECT username FROM users WHERE is_active = 1"
    cursor.execute(sql)
    result = set(row['username'] for row in cursor.fetchall())
    return result

def get_all_db_usernames(conn):
    """
    Get all usernames from the database regardless of their active status.
    This is used for synchronization with directory structure.
    """
    cursor = conn.cursor()
    sql = "SELECT username FROM users"
    logger.debug(f"Executing SQL: {sql}")
    cursor.execute(sql)
    result = set(row['username'] for row in cursor.fetchall())
    logger.debug(f"SQL result (all db usernames): {result}")
    return result

def get_next_user_to_check(conn):
    cursor = conn.cursor()
    sql = (
        "SELECT username, priority, next_check, check_interval FROM users "
        "WHERE is_live=0 "
        "AND is_active=1 "
        "AND (next_check IS NULL OR datetime('now', 'localtime') >= datetime(next_check)) "
        "ORDER BY priority ASC, COALESCE(next_check, '1970-01-01 00:00:00') ASC LIMIT 1" # Poprawiono format daty
    )
    logger.debug(f"Executing SQL: {sql}")
    cursor.execute(sql)
    row = cursor.fetchone()

    if row:
        logger.debug(f"Selected user to check: {row['username']}, Priority: {row['priority']}, Next check: {row['next_check']}, Check interval: {row['check_interval']}")
        result = row['username']
    else:
        logger.debug("No eligible user found to check")
        result = None

    return result

def get_all_users_data(conn):
    cursor = conn.cursor()
    sql = "SELECT id, username, priority, added_at FROM users WHERE is_active = 1"
    logger.debug(f"Executing SQL: {sql}")
    cursor.execute(sql)
    users = [dict(row) for row in cursor.fetchall()]
    logger.debug(f"SQL result (active users data count): {len(users)}")
    return users

def get_live_users(conn):
    """
    Get all usernames of users who have is_live = 1 in the database.
    """
    cursor = conn.cursor()
    sql = "SELECT username FROM users WHERE is_live = 1"
    logger.debug(f"Executing SQL: {sql}")
    cursor.execute(sql)
    result = [row['username'] for row in cursor.fetchall()]
    logger.debug(f"SQL result (live users count): {len(result)}")
    return result

def get_users_with_current_timestamp(conn):
    """
    Get users whose next_check is set to current local timestamp.
    Used after reset_stuck_live_users_and_prioritize_check to show which users were reset.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT username, priority, next_check, check_interval FROM users WHERE next_check = DATETIME('now', 'localtime') ORDER BY priority ASC")
    result = cursor.fetchall()
    return result

def get_eligible_users_to_check(conn, limit=10):
    """
    Get users eligible to be checked (for debugging).
    """
    cursor = conn.cursor()
    sql = (
        "SELECT username, priority, next_check, check_interval FROM users "
        "WHERE is_live=0 AND is_active=1 "
        "AND (next_check IS NULL OR datetime('now', 'localtime') >= datetime(next_check)) "
        "ORDER BY priority ASC, COALESCE(next_check, '1970-01-01 00:00:00') ASC LIMIT ?"
    )
    cursor.execute(sql, (limit,))
    return cursor.fetchall()

def reset_all_live_users_next_check(conn, interval_seconds):
    """
    Reset is_live to 0 and set next_check to now + interval_seconds for all users with is_live=1.
    Used during graceful shutdown.
    """
    cursor = conn.cursor()
    # Use parameterized query to prevent SQL injection
    # Format: "+N seconds" where N is the interval value
    interval_param = f"+{int(interval_seconds)} seconds"
    cursor.execute("""
        UPDATE lives
        SET ended_at = DATETIME('now', 'localtime')
        WHERE ended_at IS NULL
          AND user_id IN (SELECT id FROM users WHERE is_live = 1)
    """)
    closed_sessions = cursor.rowcount
    sql = "UPDATE users SET is_live = 0, next_check = DATETIME('now', ?) WHERE is_live = 1"
    logger.debug(f"Executing SQL: {sql} with param: {interval_param}")
    cursor.execute(sql, (interval_param,))
    updated_count = cursor.rowcount
    conn.commit()
    logger.info(f"Reset all live users for shutdown: users={updated_count}, sessions_closed={closed_sessions}")
    return updated_count

def end_active_live_sessions(conn):
    """
    End all active live sessions in the lives table without resetting is_live flags.
    Returns a list of live_ids that were ended.
    """
    cursor = conn.cursor()

    # Ensure row_factory is set for this connection
    if not hasattr(conn, 'row_factory') or conn.row_factory is None:
        conn.row_factory = sqlite3.Row

    # First, get all active live sessions (where ended_at is NULL)
    cursor.execute("""
        SELECT l.id, u.username
        FROM lives l
        JOIN users u ON l.user_id = u.id
        WHERE l.ended_at IS NULL
    """)
    active_sessions = cursor.fetchall()

    ended_ids = []
    for session in active_sessions:
        try:
            # Handle both Row and tuple formats
            if hasattr(session, 'keys'):  # sqlite3.Row
                live_id = session['id']
                username = session['username']
            else:  # tuple format
                live_id = session[0]
                username = session[1]

            # Update the ended_at field to current time
            ended_at = datetime.now()
            ended_at_str = ended_at.strftime("%Y-%m-%d %H:%M:%S")

            cursor.execute("UPDATE lives SET ended_at = ? WHERE id = ?", (ended_at_str, live_id))
            # Include username in the log message
            logger.info(f"Ended live session in DB for user {username}, live_id={live_id}")
            ended_ids.append(live_id)

        except Exception as e:
            logger.error(f"Error ending live session {session}: {e}")

    conn.commit()
    return ended_ids
