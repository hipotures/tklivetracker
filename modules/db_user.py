import sqlite3
from datetime import datetime
import logging
import shutil
from pathlib import Path

from utils.username import normalize_tiktok_username

logger = logging.getLogger(__name__)

def get_local_now():
    """Get current local time as datetime object"""
    return datetime.now()

def set_user_active_status(conn, username, is_active, path_config=None):
    """
    Set the active status of a user.
    Active users are processed by the supervisor, inactive users are not.
    When deactivating, also updates last_deactivated_at timestamp and moves directory.
    Recording directories are created lazily when a live recording starts.
    """
    if normalize_tiktok_username(username, strip_at=False) != username:
        raise ValueError("Invalid TikTok username")
    cursor = conn.cursor()

    recordings_path = None
    if is_active in (0, -1):
        # Use paths resolved by the owning application from its active config.
        try:
            paths = (path_config or {}).get('paths', {})
            recordings_path = paths.get('recordings_path')
            inactive_users_path = paths.get('inactive_users_path')
            if not recordings_path or not inactive_users_path:
                raise ValueError(
                    "recordings_path and inactive_users_path are required"
                )

            recordings_root = Path(recordings_path).expanduser().resolve()
            inactive_root = Path(inactive_users_path).expanduser().resolve()
            user_dir = recordings_root / username
            inactive_dir = inactive_root / username
            if user_dir.parent != recordings_root or inactive_dir.parent != inactive_root:
                raise ValueError("User path escapes its configured root")
            if user_dir.is_symlink() or inactive_dir.is_symlink():
                raise ValueError("Refusing symlinked user directory")
        except Exception as e:
            logger.error(f"Error loading config for directory management: {e}")
            # Continue with database update even if directory operations fail
            recordings_path = None

    if is_active == 0:
        # When deactivating, also update last_deactivated_at
        sql = "UPDATE users SET is_active = ?, last_deactivated_at = DATETIME('now', 'localtime') WHERE username = ?"
        logger.debug(f"Executing SQL: {sql} with params: ({is_active}, {username})")
        cursor.execute(sql, (is_active, username))

        # Move directory to inactive_users if it exists
        if recordings_path and user_dir.exists():
            try:
                inactive_root.mkdir(parents=True, exist_ok=True)

                # If destination already exists, remove it first
                if inactive_dir.exists():
                    logger.warning(f"Directory already exists in inactive_users for {username}, removing it first")
                    shutil.rmtree(inactive_dir)

                shutil.move(user_dir, inactive_dir)
                logger.info(f"Moved directory for {username} to inactive_users")
            except Exception as e:
                logger.error(f"Failed to move directory for {username}: {e}")
    elif is_active == -1:
        # When marking as deleted, update deleted_at
        sql = "UPDATE users SET is_active = ?, deleted_at = DATETIME('now', 'localtime') WHERE username = ?"
        logger.debug(f"Executing SQL: {sql} with params: ({is_active}, {username})")
        cursor.execute(sql, (is_active, username))

        # Move directory to inactive_users if it exists (same as deactivating)
        if recordings_path and user_dir.exists():
            try:
                inactive_root.mkdir(parents=True, exist_ok=True)

                # If destination already exists, remove it first
                if inactive_dir.exists():
                    logger.warning(f"Directory already exists in inactive_users for {username}, removing it first")
                    shutil.rmtree(inactive_dir)

                shutil.move(user_dir, inactive_dir)
                logger.info(f"Moved directory for {username} to inactive_users (user deleted)")
            except Exception as e:
                logger.error(f"Failed to move directory for {username}: {e}")
    else:  # is_active == 1
        sql = "UPDATE users SET is_active = ? WHERE username = ?"
        logger.debug(f"Executing SQL: {sql} with params: ({is_active}, {username})")
        cursor.execute(sql, (is_active, username))

    conn.commit()
    logger.debug(f"is_active for {username} set to {is_active}")

def add_user(conn, username, check_interval=300):
    cursor = conn.cursor()

    # First check if user already exists
    cursor.execute("SELECT is_active FROM users WHERE username = ?", (username,))
    existing = cursor.fetchone()

    if existing:
        is_active = bool(existing[0])

        if is_active:
            # User exists and is active - force immediate check
            cursor.execute("UPDATE users SET next_check = NULL WHERE username = ?", (username,))
            conn.commit()
            logger.debug(f"User {username} already exists and is active - forced immediate check")
            return {'exists': True, 'was_active': True, 'action': 'forced_check'}
        else:
            # User exists but is inactive - reactivate with default interval and immediate check
            cursor.execute("""
                UPDATE users
                SET is_active = 1,
                    check_interval = ?,
                    next_check = NULL
                WHERE username = ?
            """, (check_interval, username))
            conn.commit()
            logger.debug(f"User {username} reactivated with interval {check_interval}")
            return {'exists': True, 'was_active': False, 'action': 'reactivated'}

    # User doesn't exist - add new user
    local_time = get_local_now().strftime("%Y-%m-%d %H:%M:%S")
    sql = "INSERT INTO users (username, check_interval, added_at) VALUES (?, ?, ?)"
    logger.debug(f"Executing SQL: {sql} with params: ({username}, {check_interval}, {local_time})")
    cursor.execute(sql, (username, check_interval, local_time))
    conn.commit()
    logger.debug("User added successfully.")
    return {'exists': False, 'action': 'added'}

def set_is_live(conn, username, value):
    cursor = conn.cursor()
    sql = "UPDATE users SET is_live = ? WHERE username = ?"
    logger.debug(f"Executing SQL: {sql} with params: ({value}, {username})")
    cursor.execute(sql, (int(bool(value)), username))
    conn.commit()
    logger.debug(f"is_live for {username} set to {value}")

def update_next_check(conn, username, offset_seconds=None):
    cursor = conn.cursor()
    if offset_seconds:
        sql = "UPDATE users SET next_check = DATETIME('now', 'localtime', ? || ' seconds') WHERE username = ?"
        logger.debug(f"Executing SQL: {sql} with params: (f'+{offset_seconds}', {username})") # Poprawiono logowanie
        cursor.execute(sql, (f"+{offset_seconds}", username))
    else:
        sql = "UPDATE users SET next_check = DATETIME('now', 'localtime') WHERE username = ?"
        logger.debug(f"Executing SQL: {sql} with param: {username}")
        cursor.execute(sql, (username,))
    conn.commit()
    logger.debug(f"next_check updated for {username}.")

def force_check_now(conn, username):
    """Force immediate check by setting next_check to NULL (highest priority) for specific user"""
    cursor = conn.cursor()
    sql = "UPDATE users SET next_check = NULL WHERE username = ?"
    logger.debug(f"Executing SQL: {sql} with param: {username}")
    cursor.execute(sql, (username,))
    rowcount = cursor.rowcount
    conn.commit()
    if rowcount > 0:
        logger.info(f"Forced immediate check (highest priority) for user {username}")
        return True
    else:
        logger.warning(f"User {username} not found for force check")
        return False

def force_check_with_cache_clear(conn, username):
    """Force immediate check and signal cache clear for specific user"""
    cursor = conn.cursor()
    # Set next_check to NULL and add special marker for cache clear
    sql = "UPDATE users SET next_check = NULL WHERE username = ?"
    logger.debug(f"Executing SQL: {sql} with param: {username}")
    cursor.execute(sql, (username,))
    rowcount = cursor.rowcount
    conn.commit()
    if rowcount > 0:
        logger.info(f"Forced immediate check with cache clear for user {username}")
        return True
    else:
        logger.warning(f"User {username} not found for force check with cache clear")
        return False

def get_user_id(conn, username):
    cursor = conn.cursor()
    sql = "SELECT id FROM users WHERE username = ?"
    logger.debug(f"Executing SQL: {sql} with param: {username}")
    cursor.execute(sql, (username,))
    row = cursor.fetchone()
    user_id = row['id'] if row else None
    logger.debug(f"SQL result: {user_id}")
    return user_id


def get_reference_time(conn, user_id):
    cursor = conn.cursor()
    # Pobierz najpierw MAX(ended_at)
    sql_live = "SELECT MAX(ended_at) as last_live FROM lives WHERE user_id = ? AND ended_at IS NOT NULL"
    logger.debug(f"Executing SQL: {sql_live} with param: {user_id}")
    cursor.execute(sql_live, (user_id,))
    row_live = cursor.fetchone()
    last_live_end = row_live['last_live'] if row_live and row_live['last_live'] else None

    if last_live_end:
        # Convert string to datetime if not None
        try:
            ref_time = datetime.strptime(last_live_end, "%Y-%m-%d %H:%M:%S")
            logger.debug(f"Using last_live_end as reference time: {ref_time}")
            return ref_time
        except (ValueError, TypeError):
             logger.warning(f"Could not parse last_live_end: {last_live_end}. Falling back to added_at.")

    # If no lives or parsing error, get added_at
    sql_added = "SELECT added_at FROM users WHERE id = ?"
    logger.debug(f"Executing SQL: {sql_added} with param: {user_id}")
    cursor.execute(sql_added, (user_id,))
    row_added = cursor.fetchone()
    added_at = row_added['added_at'] if row_added else None

    if isinstance(added_at, str): # SQLite may return string
         try:
             added_at = datetime.strptime(added_at.split('.')[0], "%Y-%m-%d %H:%M:%S") # Ignore microseconds if present
         except ValueError:
             logger.error(f"Could not parse added_at string: {added_at}")
             added_at = get_local_now() # Fallback

    logger.debug(f"Using added_at as reference time: {added_at}")
    return added_at or get_local_now() # Return now() if added_at is also None

def update_user_check_interval(conn, username, interval):
    cursor = conn.cursor()
    sql = "UPDATE users SET check_interval = ? WHERE username = ?"
    cursor.execute(sql, (interval, username))
    logger.debug(f"check_interval prepared for update for {username} to {interval}.")

def get_user_active_status(conn, username):
    """
    Get the active status of a user.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT is_active FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    return bool(row[0]) if row else False

def get_user_favorite_status(conn, username):
    """
    Get the favorite status of a user.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT is_favorite FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    return bool(row[0]) if row else False

def reset_user_is_live(conn, username):
    """
    Reset is_live to 0 for a specific user.
    """
    cursor = conn.cursor()
    ended_at_str = get_local_now().strftime("%Y-%m-%d %H:%M:%S")

    # Close all active live sessions for this user before resetting is_live.
    cursor.execute("""
        UPDATE lives
        SET ended_at = ?
        WHERE user_id = (SELECT id FROM users WHERE username = ?)
          AND ended_at IS NULL
    """, (ended_at_str, username))
    closed_sessions = cursor.rowcount

    cursor.execute("UPDATE users SET is_live = 0 WHERE username = ?", (username,))
    conn.commit()
    logger.debug(f"Reset is_live=0 for {username}; closed active sessions: {closed_sessions}")

def update_next_check_with_interval(conn, username, interval_seconds):
    """
    Update next_check to now + interval_seconds for a specific user.
    """
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET next_check = DATETIME('now', 'localtime', ? || ' seconds') WHERE username = ?",
        (f"+{interval_seconds}", username)
    )
    conn.commit()

def get_user_check_interval(conn, username):
    """
    Get the check_interval for a specific user.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT check_interval FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    return row['check_interval'] if row else None

def get_user_next_check(conn, username):
    """
    Get the next_check timestamp for a specific user.
    Returns datetime object or None if not set.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT next_check FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if row and row['next_check']:
        # Handle both string and datetime objects
        next_check_value = row['next_check']
        if isinstance(next_check_value, str):
            try:
                return datetime.strptime(next_check_value, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse next_check string '{next_check_value}' for user {username}: {e}")
                return None
        elif isinstance(next_check_value, datetime):
            # Already a datetime object
            return next_check_value
        else:
            logger.warning(f"Unexpected next_check type {type(next_check_value)} for user {username}: {next_check_value}")
            return None
    return None

def get_active_live_session(conn, user_id):
    """
    Get active live session for user (where ended_at is NULL).
    Returns live session id if found, None otherwise.
    """
    cursor = conn.cursor()
    sql = "SELECT id FROM lives WHERE user_id = ? AND ended_at IS NULL"
    logger.debug(f"Executing SQL: {sql} with param: {user_id}")
    cursor.execute(sql, (user_id,))
    row = cursor.fetchone()
    live_id = row['id'] if row else None
    logger.debug(f"Active live session for user_id {user_id}: {live_id}")
    return live_id

def add_live_session(
    conn,
    user_id,
    started_at=None,
    tiktok_stream_id=None,
    tiktok_started_at=None,
    tiktok_owner_user_id=None,
):
    cursor = conn.cursor()
    owner_user_id = (
        str(tiktok_owner_user_id)
        if tiktok_owner_user_id is not None
        else None
    )
    if owner_user_id:
        cursor.execute("SELECT tt_user_id FROM users WHERE id = ?", (user_id,))
        user_row = cursor.fetchone()
        existing_user_id = user_row['tt_user_id'] if user_row else None
        if not existing_user_id:
            cursor.execute(
                "UPDATE users SET tt_user_id = ? WHERE id = ?",
                (owner_user_id, user_id),
            )
        elif str(existing_user_id) != owner_user_id:
            logger.warning(
                "TikTok owner ID mismatch for local user_id=%s: "
                "stored=%s live=%s; keeping stored value",
                user_id,
                existing_user_id,
                owner_user_id,
            )
    sql = """
        INSERT INTO lives (
            user_id, started_at, tiktok_stream_id, tiktok_started_at,
            tiktok_owner_user_id
        ) VALUES (?, ?, ?, ?, ?)
    """
    if started_at is None:
        started_at = get_local_now()
    started_at_str = started_at.strftime("%Y-%m-%d %H:%M:%S")
    tiktok_started_at_str = (
        tiktok_started_at.strftime("%Y-%m-%d %H:%M:%S")
        if tiktok_started_at is not None
        else None
    )
    params = (
        user_id,
        started_at_str,
        str(tiktok_stream_id) if tiktok_stream_id is not None else None,
        tiktok_started_at_str,
        owner_user_id,
    )
    logger.debug(f"Executing SQL: {sql} with params: {params}")
    cursor.execute(sql, params)
    conn.commit()
    live_id = cursor.lastrowid
    logger.debug(f"Live session started, id: {live_id}")
    return live_id

def end_live_session(conn, live_id, ended_at=None, reset_user_status=True):
    cursor = conn.cursor()
    logger.info(f"[DEBUG] end_live_session called: live_id={live_id}, ended_at={ended_at}")

    # First get the user_id for this live session
    cursor.execute("SELECT user_id FROM lives WHERE id = ?", (live_id,))
    row = cursor.fetchone()
    if not row:
        logger.warning(f"Live session {live_id} not found, cannot end session")
        return

    user_id = row['user_id']
    logger.info(f"[DEBUG] Found user_id={user_id} for live_id={live_id}")

    # Update the live session end time
    sql = "UPDATE lives SET ended_at = ? WHERE id = ?"
    if ended_at is None:
        ended_at = get_local_now()
    ended_at_str = ended_at.strftime("%Y-%m-%d %H:%M:%S")
    logger.debug(f"Executing SQL: {sql} with params: ({ended_at_str}, {live_id})")
    cursor.execute(sql, (ended_at_str, live_id))
    logger.info(f"[DEBUG] Updated lives table: ended_at={ended_at_str} for live_id={live_id}")

    if reset_user_status:
        cursor.execute("UPDATE users SET is_live = 0 WHERE id = ?", (user_id,))
        logger.info(f"[DEBUG] Reset users.is_live=0 for user_id={user_id}")

    # Check if database lock exists
    logger.debug(f"[DEBUG] Committing transaction for live_id={live_id}")
    conn.commit()
    logger.info(f"[DEBUG] Transaction committed successfully - Live session ended for id: {live_id}")

def delete_user(conn, username):
    """
    Delete a user and all associated live sessions.
    Returns True if user was deleted, False if user didn't exist.
    """
    cursor = conn.cursor()

    # First check if user exists
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    user_row = cursor.fetchone()
    if not user_row:
        logger.warning(f"Cannot delete user {username}: user does not exist")
        return False

    user_id = user_row['id']

    try:
        # Delete all live sessions for this user first (due to foreign key)
        cursor.execute("DELETE FROM lives WHERE user_id = ?", (user_id,))
        lives_deleted = cursor.rowcount
        logger.debug(f"Deleted {lives_deleted} live sessions for user {username}")

        # Delete the user
        cursor.execute("DELETE FROM users WHERE username = ?", (username,))
        users_deleted = cursor.rowcount

        conn.commit()
        logger.info(f"Successfully deleted user {username} and {lives_deleted} associated live sessions")
        return True

    except sqlite3.Error as e:
        logger.error(f"Error deleting user {username}: {e}")
        conn.rollback()
        return False

def update_user_properties(conn, username, path_config=None, **kwargs):
    """
    Update multiple user properties at once.
    Accepts: check_interval, is_active, is_favorite, notifications_enabled
    Special handling for is_active to update last_deactivated_at when deactivating.
    """
    cursor = conn.cursor()

    # Check if is_active is being updated
    is_active_updated = False
    if 'is_active' in kwargs:
        # Handle is_active separately to update last_deactivated_at
        is_active_value = kwargs.pop('is_active')
        is_active_updated = True
        set_user_active_status(
            conn,
            username,
            is_active_value,
            path_config=path_config,
        )

    # Build dynamic SQL for remaining fields
    valid_fields = ['check_interval', 'is_favorite', 'notifications_enabled']
    updates = []
    values = []

    for field, value in kwargs.items():
        if field in valid_fields:
            updates.append(f"{field} = ?")
            values.append(value)

    if not updates:
        # If only is_active was updated, that's still a success
        if is_active_updated:
            logger.info(f"Updated user {username}: is_active={is_active_value}")
            return True
        logger.warning(f"No valid fields provided for updating user {username}")
        return False

    sql = f"UPDATE users SET {', '.join(updates)} WHERE username = ?"
    values.append(username)

    try:
        cursor.execute(sql, values)
        conn.commit()
        if cursor.rowcount > 0:
            logger.info(f"Updated user {username}: {kwargs}")
            return True
        else:
            logger.warning(f"User {username} not found for update")
            return False
    except sqlite3.Error as e:
        logger.error(f"Error updating user {username}: {e}")
        conn.rollback()
        return False

def set_user_favorite(conn, username, is_favorite):
    """
    Set the favorite status of a user.
    """
    cursor = conn.cursor()
    sql = "UPDATE users SET is_favorite = ? WHERE username = ?"
    logger.debug(f"Executing SQL: {sql} with params: ({int(bool(is_favorite))}, {username})")
    try:
        cursor.execute(sql, (int(bool(is_favorite)), username))
        conn.commit()
        if cursor.rowcount > 0:
            logger.debug(f"is_favorite for {username} set to {is_favorite}")
            return True
        else:
            logger.warning(f"User {username} not found for favorite update")
            return False
    except sqlite3.Error as e:
        logger.error(f"Error setting favorite for {username}: {e}")
        conn.rollback()
        return False

def get_favorite_users(conn):
    """
    Get all favorite users.
    """
    cursor = conn.cursor()
    sql = """
        SELECT u.username, u.check_interval, u.is_live, u.is_active,
               u.total_lives, u.next_check, u.added_at, u.is_favorite,
               u.notifications_enabled,
               l.started_at as live_started_at,
               (
                   SELECT MAX(COALESCE(lh.ended_at, lh.started_at))
                   FROM lives lh
                   WHERE lh.user_id = u.id
               ) as last_live_at
        FROM users u
        LEFT JOIN (
            SELECT user_id, MAX(started_at) as started_at
            FROM lives
            WHERE ended_at IS NULL
            GROUP BY user_id
        ) l ON u.id = l.user_id
        WHERE u.is_favorite = 1
        ORDER BY u.is_live DESC, u.username ASC
    """
    logger.debug(f"Executing SQL: {sql}")
    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
        logger.debug(f"Found {len(rows)} favorite users")
        return rows
    except sqlite3.Error as e:
        logger.error(f"Error getting favorite users: {e}")
        return []

def get_user_notifications_status(conn, username):
    """
    Get the notifications enabled status of a user.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT notifications_enabled FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    return bool(row[0]) if row else False

def set_user_notifications(conn, username, notifications_enabled):
    """
    Set the notifications enabled status of a user.
    """
    cursor = conn.cursor()
    sql = "UPDATE users SET notifications_enabled = ? WHERE username = ?"
    logger.debug(f"Executing SQL: {sql} with params: ({int(bool(notifications_enabled))}, {username})")
    try:
        cursor.execute(sql, (int(bool(notifications_enabled)), username))
        conn.commit()
        if cursor.rowcount > 0:
            logger.debug(f"notifications_enabled for {username} set to {notifications_enabled}")
            return True
        else:
            logger.warning(f"User {username} not found for notifications update")
            return False
    except sqlite3.Error as e:
        logger.error(f"Error setting notifications for {username}: {e}")
        conn.rollback()
        return False

def get_notification_users(conn):
    """
    Get all users with notifications enabled.
    """
    cursor = conn.cursor()
    sql = """
        SELECT u.username, u.check_interval, u.is_live, u.is_active,
               u.total_lives, u.next_check, u.added_at, u.is_favorite, u.notifications_enabled,
               l.started_at as live_started_at
        FROM users u
        LEFT JOIN lives l ON u.id = l.user_id AND l.ended_at IS NULL
        WHERE u.notifications_enabled = 1
        ORDER BY u.is_live DESC, u.username ASC
    """
    logger.debug(f"Executing SQL: {sql}")
    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
        logger.debug(f"Found {len(rows)} users with notifications enabled")
        return rows
    except sqlite3.Error as e:
        logger.error(f"Error getting notification users: {e}")
        return []

def update_total_lives_count(conn, user_id):
    """
    Update the total_lives count for a specific user based on actual lives records.
    """
    cursor = conn.cursor()
    sql = """
        UPDATE users
        SET total_lives = (
            SELECT COUNT(*)
            FROM lives
            WHERE lives.user_id = ?
            AND lives.ended_at IS NOT NULL
        )
        WHERE id = ?
    """
    try:
        cursor.execute(sql, (user_id, user_id))
        conn.commit()
        logger.debug(f"Updated total_lives count for user_id {user_id}")
        return True
    except sqlite3.Error as e:
        logger.error(f"Error updating total_lives for user_id {user_id}: {e}")
        conn.rollback()
        return False
