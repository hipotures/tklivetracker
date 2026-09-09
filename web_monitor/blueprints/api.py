import os
import shutil
import stat
from pathlib import Path
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, current_app

# Import utilities
from web_monitor.utils.database import get_db
from web_monitor.utils.validation import sanitize_username
from web_monitor.utils.write_access import require_write_access

# Import database functions
from modules.db_user import (
    add_user, delete_user, update_user_properties,
    set_user_favorite,
    get_favorite_users, force_check_now,
    set_user_notifications, get_notification_users
)
from modules.favorite_links import format_sync_report, sync_favorite_links
from modules.user_data_transformer import user_data_transformer
from modules.supervisor_status import SupervisorStatusManager

# Create blueprint
api_bp = Blueprint('api', __name__, url_prefix='/api')


def resolve_user_recordings_directory(recordings_root, username):
    """Resolve one direct, non-symlink child below the recordings root."""
    normalized = sanitize_username(username)
    if normalized != username:
        raise ValueError("Invalid username format")

    root = Path(recordings_root).resolve()
    user_path = root / normalized
    if user_path == root or user_path.parent != root:
        raise ValueError("User recordings path escapes the recordings root")
    try:
        if stat.S_ISLNK(user_path.lstat().st_mode):
            raise ValueError("Refusing to delete a symlinked recordings directory")
    except FileNotFoundError:
        pass
    return user_path


def parse_bounded_int(value, field_name, minimum, maximum):
    """Parse an integer supplied by a client and enforce a safe range."""
    if isinstance(value, bool):
        raise ValueError(f'{field_name} must be an integer')
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f'{field_name} must be an integer')
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or not stripped.lstrip('+-').isdigit():
            raise ValueError(f'{field_name} must be an integer')

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f'{field_name} must be an integer') from None

    if parsed < minimum or parsed > maximum:
        raise ValueError(
            f'{field_name} must be between {minimum} and {maximum}'
        )
    return parsed


def parse_boolean(value, field_name):
    """Accept JSON booleans (and legacy 0/1 values) without truthy-string bugs."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f'{field_name} must be a boolean')

def sync_favorite_links_for_request(db):
    favorite_source_path = current_app.config.get('FAVORITE_SOURCE_PATH')
    recordings_fav_path = current_app.config.get('RECORDINGS_FAV_PATH')

    if (
        not favorite_source_path
        or not recordings_fav_path
    ):
        current_app.logger.warning("Favorite link sync skipped: recordings paths are not configured")
        return None

    report = sync_favorite_links(db, favorite_source_path, recordings_fav_path)
    if report.has_changes():
        current_app.logger.info("Favorite link sync:\n%s", format_sync_report(report))
    else:
        current_app.logger.debug("Favorite link sync: already in sync")
    return report

@api_bp.route('/dashboard', methods=['GET'])
def get_dashboard_data():
    """Get dashboard statistics and live users"""
    try:
        db = get_db()

        # Get live users with actual recording process start time and previous live session info
        live_query = """
            SELECT u.username, u.check_interval, u.is_live, u.is_active,
                   u.total_lives, u.next_check, u.added_at, u.is_favorite, u.notifications_enabled,
                   COALESCE(p.started_at, MIN(l.started_at)) as live_started_at,
                   CASE WHEN p.username IS NOT NULL THEN 1 ELSE 0 END as is_recording,
                   prev_live.started_at as previous_live_started_at,
                   prev_live.ended_at as previous_live_ended_at
            FROM users u
            LEFT JOIN lives l ON u.id = l.user_id AND l.ended_at IS NULL
            LEFT JOIN (
                SELECT username, MIN(started_at) AS started_at
                FROM live_processes
                WHERE is_active = 1
                GROUP BY username
            ) p ON u.username = p.username
            LEFT JOIN (
                SELECT user_id, started_at, ended_at,
                       ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ended_at DESC) as rn
                FROM lives
                WHERE ended_at IS NOT NULL
            ) prev_live ON u.id = prev_live.user_id AND prev_live.rn = 1
            WHERE u.is_live = 1
            GROUP BY u.username, u.check_interval, u.is_live, u.is_active,
                     u.total_lives, u.next_check, u.added_at, u.is_favorite, u.notifications_enabled,
                     p.started_at, prev_live.started_at, prev_live.ended_at
            ORDER BY u.is_favorite DESC,
                     COALESCE(p.started_at, MIN(l.started_at)) DESC
        """
        live_user_rows = db.execute(live_query).fetchall()
        live_users = [user_data_transformer.create_live_user_summary(row) for row in live_user_rows]

        # Get statistics
        stats_query = """
            SELECT
                COUNT(*) as total_users,
                SUM(CASE WHEN is_live = 1 THEN 1 ELSE 0 END) as live_users,
                SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) as active_users,
                SUM(total_lives) as total_recorded_lives
            FROM users
        """
        stats = dict(db.execute(stats_query).fetchone())

        # Count long streams (>5 hours)
        long_stream_threshold_seconds = 5 * 3600  # 5 hours in seconds
        long_streams_count = 0
        for user in live_users:
            if user.get('live_duration') and isinstance(user['live_duration'], (int, float)):
                if user['live_duration'] >= long_stream_threshold_seconds:
                    long_streams_count += 1

        stats['long_streams'] = long_streams_count

        return jsonify({
            'live_users': live_users,
            'statistics': stats,
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })

    except Exception as e:
        current_app.logger.error(f"Dashboard data error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@api_bp.route('/users', methods=['GET'])
def get_users():
    """Get users with pagination, search, and filtering"""
    try:
        db = get_db()

        # Get query parameters
        page = parse_bounded_int(request.args.get('page', 1), 'page', 1, 1_000_000)
        per_page = parse_bounded_int(request.args.get('per_page', 50), 'per_page', 1, 100)
        search = request.args.get('search', '').strip()
        live_filter = request.args.get('live_filter', 'all')  # all, live, offline
        active_filter = request.args.get('active_filter', 'all')  # all, active, inactive, notifications_enabled, notifications_disabled
        sort_by = request.args.get('sort_by', 'username')  # username, interval, live, active, total_lives, next_check
        sort_order = request.args.get('sort_order', 'asc')  # asc, desc

        if len(search) > 255:
            raise ValueError('search must be 255 characters or fewer')

        if live_filter not in ['all', 'live', 'offline']:
            raise ValueError('live_filter must be all, live, or offline')

        if active_filter not in ['all', 'active', 'inactive', 'notifications_enabled', 'notifications_disabled']:
            raise ValueError('active_filter has an unsupported value')

        # Build WHERE clause
        where_conditions = []
        params = []

        if search:
            where_conditions.append("u.username LIKE ?")
            params.append(f'%{search}%')

        if live_filter != 'all':
            where_conditions.append("u.is_live = ?")
            params.append(1 if live_filter == 'live' else 0)

        if active_filter in ['active', 'inactive']:
            where_conditions.append("u.is_active = ?")
            params.append(1 if active_filter == 'active' else 0)
        elif active_filter in ['notifications_enabled', 'notifications_disabled']:
            where_conditions.append("u.notifications_enabled = ?")
            params.append(1 if active_filter == 'notifications_enabled' else 0)

        where_clause = ' AND '.join(where_conditions)
        if where_clause:
            where_clause = 'WHERE ' + where_clause

        # Build ORDER BY clause with whitelist validation
        sort_column_map = {
            'username': 'u.username',
            'interval': 'u.check_interval',
            'live': 'u.is_live',
            'active': 'u.is_active',
            'total_lives': 'u.total_lives',
            'next_check': 'u.next_check'
        }

        # Validate sort_by parameter against whitelist
        if sort_by not in sort_column_map:
            sort_by = 'username'
        sort_column = sort_column_map[sort_by]

        # Validate sort_order parameter
        if sort_order not in ['asc', 'desc']:
            sort_order = 'asc'
        order_direction = 'DESC' if sort_order == 'desc' else 'ASC'

        # Get total count
        count_query = f"""
            SELECT COUNT(*) as total
            FROM users u
            {where_clause}
        """
        total_users = db.execute(count_query, params).fetchone()['total']

        # Build ORDER BY clause safely
        if sort_by == 'next_check':
            order_clause = f"ORDER BY CASE WHEN {sort_column} IS NULL THEN 0 ELSE 1 END {order_direction}, {sort_column} {order_direction}"
        else:
            order_clause = f"ORDER BY {sort_column} {order_direction}"

        # Paginate users first; joining all live rows before LIMIT is slow on large databases.
        offset = (page - 1) * per_page
        users_query = f"""
            WITH page_users AS (
                SELECT u.id, u.username, u.check_interval, u.is_live, u.is_active,
                       u.total_lives, u.next_check, u.added_at, u.is_favorite, u.notifications_enabled
                FROM users u
                {where_clause}
                {order_clause}
                LIMIT ? OFFSET ?
            )
            SELECT pu.username, pu.check_interval, pu.is_live, pu.is_active,
                   pu.total_lives, pu.next_check, pu.added_at, pu.is_favorite, pu.notifications_enabled,
                   (
                       SELECT MIN(l.started_at)
                       FROM lives l
                       WHERE l.user_id = pu.id AND l.ended_at IS NULL
                   ) as live_started_at
            FROM page_users pu
        """

        user_rows = db.execute(users_query, params + [per_page, offset]).fetchall()
        users = user_data_transformer.transform_multiple_user_rows(user_rows, include_live_duration=True)

        pagination = user_data_transformer.create_pagination_info(page, per_page, total_users)

        return jsonify(user_data_transformer.format_api_response(users, pagination))

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Get users error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@api_bp.route('/users', methods=['POST'])
@require_write_access
def create_user():
    """Add a new user"""
    try:
        data = request.get_json()
        if not isinstance(data, dict) or 'username' not in data:
            return jsonify({'error': 'Username is required'}), 400

        username_raw = data['username']
        username_sanitized = sanitize_username(username_raw)

        if not username_sanitized:
            return jsonify({'error': 'Invalid username format'}), 400

        try:
            user_dir_path = resolve_user_recordings_directory(
                current_app.config['RECORDINGS_PATH'], username_sanitized
            )
        except ValueError as error:
            return jsonify({'error': str(error)}), 400
        user_dir_path.mkdir(parents=True, exist_ok=True)

        db = get_db()
        config_intervals = current_app.config['CONFIG'].get('intervals', {})
        default_interval = config_intervals.get('default_check_interval', 300)

        result = add_user(db, username_sanitized, check_interval=default_interval)

        if result['exists']:
            if result['was_active']:
                return jsonify({
                    'message': f'User {username_sanitized} already exists and is active - will check immediately',
                    'username': username_sanitized,
                    'action': 'forced_check'
                })
            else:
                return jsonify({
                    'message': f'User {username_sanitized} was inactive - reactivated and will check immediately',
                    'username': username_sanitized,
                    'action': 'reactivated'
                })
        else:
            return jsonify({
                'message': f'User {username_sanitized} added successfully',
                'username': username_sanitized,
                'action': 'added'
            })

    except Exception as e:
        current_app.logger.error(f"Create user error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@api_bp.route('/users/<username>', methods=['GET'])
def get_user(username):
    """Get a single user by username"""
    try:
        if sanitize_username(username) != username:
            return jsonify({'error': 'Invalid username format'}), 400
        db = get_db()
        user_query = """
            SELECT u.username, u.check_interval, u.is_live, u.is_active,
                   u.total_lives, u.next_check, u.added_at, u.is_favorite, u.notifications_enabled,
                   MIN(l.started_at) as live_started_at
            FROM users u
            LEFT JOIN lives l ON u.id = l.user_id AND l.ended_at IS NULL
            WHERE u.username = ?
            GROUP BY u.username, u.check_interval, u.is_live, u.is_active,
                     u.total_lives, u.next_check, u.added_at, u.is_favorite, u.notifications_enabled
        """
        row = db.execute(user_query, (username,)).fetchone()

        if not row:
            return jsonify({'error': 'User not found'}), 404

        user = user_data_transformer.transform_user_row(row, include_live_duration=True)
        return jsonify({'user': user})

    except Exception as e:
        current_app.logger.error(f"Get user error for {username}: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@api_bp.route('/users/<username>', methods=['PUT'])
@require_write_access
def update_user(username):
    """Update user properties"""
    try:
        if sanitize_username(username) != username:
            return jsonify({'error': 'Invalid username format'}), 400
        data = request.get_json()
        if not isinstance(data, dict) or not data:
            return jsonify({'error': 'No data provided'}), 400

        # Validate the entire request before any database or filesystem mutation.
        valid_updates = {}
        if 'check_interval' in data:
            check_interval = parse_bounded_int(
                data['check_interval'], 'Check interval', 1, 3600
            )
            valid_updates['check_interval'] = check_interval
        if 'is_active' in data:
            valid_updates['is_active'] = int(parse_boolean(data['is_active'], 'is_active'))
        if 'is_favorite' in data:
            valid_updates['is_favorite'] = int(parse_boolean(data['is_favorite'], 'is_favorite'))
        if 'notifications_enabled' in data:
            valid_updates['notifications_enabled'] = int(
                parse_boolean(data['notifications_enabled'], 'notifications_enabled')
            )

        if not valid_updates:
            return jsonify({'error': 'No valid fields to update'}), 400

        db = get_db()
        if valid_updates.get('is_active') == 0:
            # Delay checks before deactivation when a recording is active.
            future_time = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
            db.execute(
                "UPDATE users SET next_check = ? WHERE username = ? "
                "AND EXISTS (SELECT 1 FROM live_processes "
                "WHERE username = ? AND is_active = 1)",
                (future_time, username, username),
            )
            db.commit()

        success = update_user_properties(
            db,
            username,
            path_config=current_app.config['CONFIG'],
            **valid_updates,
        )

        if success:
            if 'is_favorite' in valid_updates:
                state = 'enabled' if valid_updates['is_favorite'] else 'disabled'
                current_app.logger.info("Favorite %s: %s", state, username)
            if 'is_favorite' in valid_updates or 'is_active' in valid_updates:
                sync_favorite_links_for_request(db)
            return jsonify({'message': f'User {username} updated successfully'})
        else:
            return jsonify({'error': 'User not found'}), 404

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Update user error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@api_bp.route('/users/<username>', methods=['DELETE'])
@require_write_access
def delete_user_endpoint(username):
    """Delete a user and their recordings directory"""
    try:
        try:
            user_dir_path = resolve_user_recordings_directory(
                current_app.config['RECORDINGS_PATH'], username
            )
        except ValueError as error:
            return jsonify({'error': str(error)}), 400

        db = get_db()

        user = db.execute(
            "SELECT is_live FROM users WHERE username = ?", (username,)
        ).fetchone()
        if user is None:
            return jsonify({'error': 'User not found'}), 404
        active_process = db.execute(
            "SELECT 1 FROM live_processes "
            "WHERE username = ? AND is_active = 1 LIMIT 1",
            (username,),
        ).fetchone()
        if user['is_live'] or active_process is not None:
            return jsonify({'error': 'Cannot delete a user with an active recording'}), 409

        # Delete from database first
        success = delete_user(db, username)

        if not success:
            return jsonify({'error': 'User not found'}), 404

        # Delete user directory if it exists
        if user_dir_path.exists():
            try:
                shutil.rmtree(user_dir_path)
            except OSError as e:
                current_app.logger.warning(f"Could not delete directory {user_dir_path}: {e}")

        sync_favorite_links_for_request(db)

        return jsonify({'message': f'User {username} deleted successfully'})

    except Exception as e:
        current_app.logger.error(f"Delete user error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@api_bp.route('/favorites', methods=['GET'])
def get_favorites():
    """Get all favorite users"""
    try:
        db = get_db()
        favorite_rows = get_favorite_users(db)

        favorites = user_data_transformer.transform_multiple_user_rows(favorite_rows, include_live_duration=True)

        return jsonify({
            'favorites': favorites,
            'count': len(favorites)
        })

    except Exception as e:
        current_app.logger.error(f"Get favorites error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@api_bp.route('/users/<username>/favorite', methods=['PUT'])
@require_write_access
def toggle_user_favorite(username):
    """Toggle user favorite status"""
    try:
        if sanitize_username(username) != username:
            return jsonify({'error': 'Invalid username format'}), 400
        data = request.get_json()
        if not isinstance(data, dict) or 'is_favorite' not in data:
            return jsonify({'error': 'is_favorite field is required'}), 400

        is_favorite = parse_boolean(data['is_favorite'], 'is_favorite')

        db = get_db()
        success = set_user_favorite(db, username, is_favorite)

        if success:
            state = 'enabled' if is_favorite else 'disabled'
            current_app.logger.info("Favorite %s: %s", state, username)
            sync_favorite_links_for_request(db)
            action = 'added to' if is_favorite else 'removed from'
            return jsonify({
                'message': f'User {username} {action} favorites',
                'is_favorite': is_favorite
            })
        else:
            return jsonify({'error': 'User not found'}), 404

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Toggle favorite error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@api_bp.route('/users/<username>/notifications', methods=['PUT'])
@require_write_access
def toggle_user_notifications(username):
    """Toggle user notifications status"""
    try:
        if sanitize_username(username) != username:
            return jsonify({'error': 'Invalid username format'}), 400
        data = request.get_json()
        if not isinstance(data, dict) or 'notifications_enabled' not in data:
            return jsonify({'error': 'notifications_enabled field is required'}), 400

        notifications_enabled = parse_boolean(
            data['notifications_enabled'], 'notifications_enabled'
        )

        db = get_db()
        success = set_user_notifications(db, username, notifications_enabled)

        if success:
            action = 'enabled' if notifications_enabled else 'disabled'
            return jsonify({
                'message': f'Notifications {action} for {username}',
                'notifications_enabled': notifications_enabled
            })
        else:
            return jsonify({'error': 'User not found'}), 404

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Toggle notifications error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@api_bp.route('/notifications', methods=['GET'])
def get_notification_users_endpoint():
    """Get all users with notifications enabled"""
    try:
        db = get_db()
        notification_rows = get_notification_users(db)

        notifications = user_data_transformer.transform_multiple_user_rows(notification_rows, include_live_duration=True)

        return jsonify({
            'notification_users': notifications,
            'count': len(notifications)
        })

    except Exception as e:
        current_app.logger.error(f"Get notification users error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@api_bp.route('/users/<username>/check', methods=['POST'])
@require_write_access
def force_user_check(username):
    """Force immediate live check for specific user"""
    try:
        if sanitize_username(username) != username:
            return jsonify({'error': 'Invalid username format'}), 400
        db = get_db()
        success = force_check_now(db, username)

        if success:
            return jsonify({
                'success': True,
                'message': f'Immediate check scheduled for {username}'
            })
        else:
            return jsonify({
                'success': False,
                'message': 'User not found'
            }), 404

    except Exception as e:
        current_app.logger.error(f"Force check error for {username}: {e}")
        return jsonify({
            'success': False,
            'message': 'Internal server error'
        }), 500

@api_bp.route('/new-users', methods=['GET'])
def get_new_users():
    """Get newest users with configurable limit and sorting"""
    try:
        db = get_db()

        # Get parameters
        limit = int(request.args.get('limit', 10))
        sort_by = request.args.get('sort_by', 'added_at')
        sort_order = request.args.get('sort_order', 'desc')

        # Validate parameters
        if limit not in [10, 25, 50]:
            limit = 10

        valid_sort_fields = {
            'username': 'username',
            'added_at': 'added_at',
            'check_interval': 'check_interval',
            'total_lives': 'total_lives'
        }

        if sort_by not in valid_sort_fields:
            sort_by = 'added_at'
        safe_sort_column = valid_sort_fields[sort_by]

        if sort_order not in ['asc', 'desc']:
            sort_order = 'desc'

        # Build query
        query = f"""
            SELECT
                username,
                added_at,
                check_interval,
                total_lives,
                is_active,
                is_live,
                is_favorite,
                notifications_enabled
            FROM users
            ORDER BY {safe_sort_column} {sort_order.upper()}
            LIMIT ?
        """

        results = db.execute(query, [limit]).fetchall()

        new_users = []
        for row in results:
            new_users.append({
                'username': row['username'],
                'added_at': row['added_at'],
                'check_interval': row['check_interval'],
                'total_lives': row['total_lives'],
                'is_active': bool(row['is_active']),
                'is_live': bool(row['is_live']),
                'is_favorite': bool(row['is_favorite']),
                'notifications_enabled': bool(row['notifications_enabled'])
            })

        return jsonify({
            'success': True,
            'users': new_users,
            'limit': limit,
            'sort_by': sort_by,
            'sort_order': sort_order,
            'count': len(new_users),
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })

    except Exception as e:
        current_app.logger.error(f"New users error: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

@api_bp.route('/recent-live-users', methods=['GET'])
def get_recent_live_users():
    """Get users who had live sessions recently, including current live users"""
    try:
        db = get_db()

        # Get parameters
        limit = int(request.args.get('limit', 10))
        sort_by = request.args.get('sort_by', 'last_live_at')
        sort_order = request.args.get('sort_order', 'desc')

        # Validate parameters
        if limit not in [10, 25, 50]:
            limit = 10

        valid_sort_fields = {
            'username': 'u.username',
            'last_live_at': 'last_live_at',
            'check_interval': 'u.check_interval',
            'total_lives': 'u.total_lives'
        }

        if sort_by not in valid_sort_fields:
            sort_by = 'last_live_at'
        safe_sort_column = valid_sort_fields[sort_by]

        if sort_order not in ['asc', 'desc']:
            sort_order = 'desc'

        # Build query to get users with their most recent live session
        # Uses subquery to get only the latest session per user and check if it's currently active
        query = f"""
            SELECT
                u.username,
                u.check_interval,
                u.total_lives,
                u.is_active,
                u.is_live,
                u.is_favorite,
                u.notifications_enabled,
                latest.started_at as last_live_at,
                CASE WHEN latest.ended_at IS NULL THEN 1 ELSE 0 END as is_currently_live
            FROM users u
            JOIN (
                SELECT
                    l.user_id,
                    l.started_at,
                    l.ended_at,
                    ROW_NUMBER() OVER (PARTITION BY l.user_id ORDER BY l.started_at DESC) as rn
                FROM lives l
            ) latest ON u.id = latest.user_id AND latest.rn = 1
            ORDER BY {safe_sort_column} {sort_order.upper()}
            LIMIT ?
        """

        results = db.execute(query, [limit]).fetchall()

        recent_live_users = []
        for row in results:
            recent_live_users.append({
                'username': row['username'],
                'last_live_at': row['last_live_at'],
                'check_interval': row['check_interval'],
                'total_lives': row['total_lives'],
                'is_active': bool(row['is_active']),
                'is_live': bool(row['is_live']),
                'is_favorite': bool(row['is_favorite']),
                'notifications_enabled': bool(row['notifications_enabled']),
                'is_currently_live': bool(row['is_currently_live'])
            })

        return jsonify({
            'success': True,
            'users': recent_live_users,
            'limit': limit,
            'sort_by': sort_by,
            'sort_order': sort_order,
            'count': len(recent_live_users),
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })

    except Exception as e:
        current_app.logger.error(f"Recent live users error: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

@api_bp.route('/supervisor-status', methods=['GET'])
def get_supervisor_status():
    """Get basic supervisor status for header indicators"""
    try:
        config = current_app.config.get('CONFIG', {})
        status = SupervisorStatusManager.get_supervisor_status(config)

        return jsonify({
            'success': True,
            'status': status['status'],
            'message': status['message'],
            'last_seen_minutes': status.get('minutes_ago', 0)
        })

    except Exception as e:
        current_app.logger.error(f"Supervisor status error: {e}")
        return jsonify({
            'success': False,
            'status': 'error',
            'message': 'Failed to get supervisor status'
        }), 500

@api_bp.route('/config', methods=['GET'])
def get_config():
    """Get application configuration including read-only status"""
    return jsonify({
        'read_only': current_app.config.get('READ_ONLY', False)
    })

@api_bp.route('/supervisor-details', methods=['GET'])
def get_supervisor_details():
    """Get detailed supervisor information for Admin page"""
    try:
        config = current_app.config.get('CONFIG', {})
        status = SupervisorStatusManager.get_supervisor_status(config)

        # Calculate uptime if supervisor is running
        uptime = None
        if status.get('started_at'):
            try:
                # Handle both string and datetime formats
                if isinstance(status['started_at'], str):
                    started = datetime.strptime(status['started_at'], '%Y-%m-%d %H:%M:%S')
                else:
                    started = status['started_at']
                now = datetime.now()
                uptime_delta = now - started

                days = uptime_delta.days
                hours, remainder = divmod(uptime_delta.seconds, 3600)
                minutes, seconds = divmod(remainder, 60)

                if days > 0:
                    uptime = f"{days}d {hours}h {minutes}m"
                elif hours > 0:
                    uptime = f"{hours}h {minutes}m"
                else:
                    uptime = f"{minutes}m {seconds}s"
            except:
                uptime = "Unknown"

        # Get system information with resolved paths
        db_path = current_app.config.get('DATABASE', '--')
        recordings_path = current_app.config.get('RECORDINGS_PATH', '--')

        log_path = config.get('paths', {}).get('log_path', '--')

        # Get additional config parameters
        intervals = config.get('intervals', {})
        persistent_live = config.get('persistent_live_system', {})
        read_only = current_app.config.get('READ_ONLY', False)
        hidden = '[hidden in read-only mode]'

        return jsonify({
            'success': True,
            'supervisor': {
                'status': status['status'],
                'pid': hidden if read_only else status.get('pid', '--'),
                'started_at': status.get('started_at', '--'),
                'last_heartbeat': status.get('last_heartbeat', '--'),
                'version': status.get('version', '--'),
                'config_hash': hidden if read_only else status.get('config_hash', '--'),
                'uptime': uptime or '--',
                'message': status['message']
            },
            'stats': status.get('stats', {}),
            'system': {
                'database': {
                    'path': hidden if read_only else db_path,
                    'connection': 'Connected' if status['status'] != 'error' else 'Error'
                },
                'config': {
                    'recordings_path': hidden if read_only else recordings_path,
                    'log_path': hidden if read_only else log_path,
                    'check_new_users': intervals.get('check_new_users', '--'),
                    'default_check_interval': intervals.get('default_check_interval', '--'),
                    'max_check_interval': intervals.get('max_check_interval', '--'),
                    'after_live_check_interval': intervals.get('after_live_check_interval', '--'),
                    'supervisor_heartbeat_interval': intervals.get('supervisor_heartbeat_interval', '--'),
                    'health_check_interval': persistent_live.get('health_check_interval', '--'),
                    'max_live_processes': persistent_live.get('max_live_processes', '--'),
                    'detached_process_mode': persistent_live.get('detached_process_mode', '--')
                }
            },
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })

    except Exception as e:
        current_app.logger.error(f"Supervisor details error: {e}")
        return jsonify({
            'success': False,
            'error': 'Failed to get supervisor details'
        }), 500


@api_bp.route('/database-statistics', methods=['GET'])
def get_database_statistics():
    """Return on-demand aggregate statistics for the configured SQLite database."""
    try:
        db = get_db()
        database_path = Path(current_app.config['DATABASE'])
        size_bytes = sum(
            path.stat().st_size
            for path in (
                database_path,
                Path(f"{database_path}-wal"),
                Path(f"{database_path}-shm"),
            )
            if path.is_file()
        )
        table_count = db.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]

        counts = {}
        for name, query in {
            'users': "SELECT COUNT(*) FROM users",
            'lives': "SELECT COUNT(*) FROM lives",
            'process_records': "SELECT COUNT(*) FROM live_processes",
            'active_recorders': (
                "SELECT COUNT(*) FROM live_processes WHERE is_active = 1"
            ),
        }.items():
            counts[name] = db.execute(query).fetchone()[0]

        return jsonify({
            'success': True,
            'database': {
                'size_bytes': size_bytes,
                'table_count': table_count,
                **counts,
            },
        })
    except Exception as e:
        current_app.logger.error(f"Database statistics error: {e}")
        return jsonify({
            'success': False,
            'error': 'Failed to load database statistics',
        }), 500
