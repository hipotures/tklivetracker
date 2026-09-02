from datetime import datetime
from flask import Blueprint, jsonify, request, current_app

# Import utilities
from web_monitor.utils.database import get_db
from web_monitor.utils.write_access import require_write_access

# Import database functions
from modules.db_user import update_user_properties
from modules.user_data_transformer import user_data_transformer

# Create blueprint
stats_bp = Blueprint('stats', __name__, url_prefix='/api')

@stats_bp.route('/stats', methods=['GET'])
def get_stats():
    """Get comprehensive system statistics and user delays"""
    try:
        db = get_db()
        active_only = request.args.get('active_only', 'true').lower() == 'true'
        sort_by = request.args.get('sort_by', 'delay_seconds')
        sort_order = request.args.get('sort_order', 'desc')
        limit = int(request.args.get('limit', 10))

        # Validate limit
        if limit not in [10, 25, 50]:
            limit = 10

        current_time = datetime.now()

        # Base query conditions
        where_conditions = []
        params = []

        if active_only:
            where_conditions.append("is_active = 1")

        # Exclude live users from delay calculations but count them separately
        where_conditions.append("is_live = 0")
        where_clause = " AND ".join(where_conditions)

        # Validate sort parameters with whitelist
        valid_sort_fields = {
            'username': 'username',
            'check_interval': 'check_interval',
            'next_check': 'next_check',
            'delay_seconds': 'delay_seconds'
        }

        if sort_by not in valid_sort_fields:
            sort_by = 'next_check'
        safe_sort_column = valid_sort_fields[sort_by]

        # Validate sort order
        if sort_order not in ['asc', 'desc']:
            sort_order = 'asc'
        sort_direction = 'ASC' if sort_order == 'asc' else 'DESC'

        # Special handling for NULL next_check values
        if sort_by == 'next_check':
            order_clause = f"""
                ORDER BY
                    CASE WHEN {safe_sort_column} IS NULL THEN 0 ELSE 1 END {sort_direction},
                    {safe_sort_column} {sort_direction}
            """
        else:
            order_clause = f"ORDER BY {safe_sort_column} {sort_direction}"

        # Get delay statistics for ALL users first
        delay_stats_query = f"""
            SELECT CASE
                       WHEN next_check IS NULL THEN 0
                       WHEN datetime(next_check) <= datetime('now', 'localtime') THEN
                           CAST((julianday('now', 'localtime') - julianday(next_check)) * 86400 AS INTEGER)
                       ELSE 0
                   END as delay_seconds
            FROM users
            WHERE {where_clause}
        """

        delay_stats = {
            'no_delay': 0,
            'delay_0_60': 0,
            'delay_60_120': 0,
            'delay_120_180': 0,
            'delay_180_plus': 0
        }

        # Calculate delay statistics for all users
        total_processed = 0
        for row in db.execute(delay_stats_query, params).fetchall():
            delay_seconds = row['delay_seconds']
            total_processed += 1
            if delay_seconds == 0:
                delay_stats['no_delay'] += 1
            elif delay_seconds <= 60:
                delay_stats['delay_0_60'] += 1
            elif delay_seconds <= 120:
                delay_stats['delay_60_120'] += 1
            elif delay_seconds <= 180:
                delay_stats['delay_120_180'] += 1
            else:
                delay_stats['delay_180_plus'] += 1

        # Get users with delays for the table
        delay_query = f"""
            SELECT username, check_interval, next_check, is_live, is_active, is_favorite,
                   CASE
                       WHEN next_check IS NULL THEN 0
                       WHEN datetime(next_check) <= datetime('now', 'localtime') THEN
                           CAST((julianday('now', 'localtime') - julianday(next_check)) * 86400 AS INTEGER)
                       ELSE 0
                   END as delay_seconds
            FROM users
            WHERE {where_clause}
            {order_clause}
            LIMIT ?
        """

        users_with_delays = []

        for row in db.execute(delay_query, params + [limit]).fetchall():
            delay_seconds = row['delay_seconds']
            user_data = {
                'username': row['username'],
                'check_interval': row['check_interval'],
                'next_check': row['next_check'],
                'delay_seconds': delay_seconds,
                'is_active': bool(row['is_active']),
                'is_favorite': bool(row['is_favorite']),
                'delay_category': 'no_delay'
            }

            # Categorize delays (for display only, not counting)
            if delay_seconds == 0:
                user_data['delay_category'] = 'no_delay'
            elif delay_seconds <= 60:
                user_data['delay_category'] = 'delay_0_60'
            elif delay_seconds <= 120:
                user_data['delay_category'] = 'delay_60_120'
            elif delay_seconds <= 180:
                user_data['delay_category'] = 'delay_120_180'
            else:
                user_data['delay_category'] = 'delay_180_plus'

            users_with_delays.append(user_data)

        # Get live users count (always for all users)
        live_count_query = "SELECT COUNT(*) as count FROM users WHERE is_live = 1"
        live_users_count = db.execute(live_count_query).fetchone()['count']

        # Get system statistics (always for all users for System Overview)
        system_stats_query = """
            SELECT
                COUNT(*) as total_users,
                SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) as active_users,
                SUM(CASE WHEN is_live = 1 THEN 1 ELSE 0 END) as live_users,
                AVG(check_interval) as avg_interval,
                MIN(check_interval) as min_interval,
                MAX(check_interval) as max_interval,
                SUM(total_lives) as total_recorded_lives
            FROM users
        """
        system_stats = dict(db.execute(system_stats_query).fetchone())

        # Get interval distribution using safe conditional logic
        if active_only:
            interval_dist_query = """
                SELECT check_interval, COUNT(*) as count
                FROM users
                WHERE is_active = 1
                GROUP BY check_interval
                ORDER BY count DESC
            """
        else:
            interval_dist_query = """
                SELECT check_interval, COUNT(*) as count
                FROM users
                GROUP BY check_interval
                ORDER BY count DESC
            """
        interval_distribution = []
        for row in db.execute(interval_dist_query).fetchall():
            interval_distribution.append({
                'interval': row['check_interval'],
                'count': row['count']
            })

        # Calculate estimated cycle time (always for all active users)
        active_non_live_users = system_stats['active_users'] - system_stats['live_users']
        config_intervals = current_app.config['CONFIG'].get('intervals', {})
        min_user_interval = config_intervals.get('min_user_interval', 1)
        estimated_cycle_seconds = int(round(active_non_live_users * min_user_interval))

        # Format cycle time
        if estimated_cycle_seconds >= 3600:
            hours, remainder = divmod(estimated_cycle_seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            cycle_time_formatted = f"{hours}h {minutes}m"
        elif estimated_cycle_seconds >= 60:
            minutes = estimated_cycle_seconds // 60
            seconds = estimated_cycle_seconds % 60
            cycle_time_formatted = f"{minutes}m {seconds}s"
        else:
            cycle_time_formatted = f"{estimated_cycle_seconds}s"

        # Get favorites count (always for all users)
        favorites_query = "SELECT COUNT(*) as count FROM users WHERE is_favorite = 1"
        favorites_count = db.execute(favorites_query).fetchone()['count']

        # Get live processes capacity information from database
        active_processes_query = "SELECT COUNT(*) as count FROM live_processes WHERE is_active = 1"
        active_processes_count = db.execute(active_processes_query).fetchone()['count']

        # Get max processes limit from config
        max_live_processes = current_app.config['CONFIG'].get('persistent_live_system', {}).get('max_live_processes', 20)
        capacity_percentage = (active_processes_count / max_live_processes) * 100 if max_live_processes > 0 else 0

        # Determine capacity status color for web interface
        if capacity_percentage >= 100:
            capacity_status = 'red'  # At limit
        elif capacity_percentage >= 80:
            capacity_status = 'yellow'  # Warning
        else:
            capacity_status = 'green'  # Normal

        return jsonify({
            'delay_stats': delay_stats,
            'users_with_delays': users_with_delays,
            'live_users_count': live_users_count,
            'system_stats': {
                'total_users': system_stats['total_users'],
                'active_users': system_stats['active_users'],
                'live_users': system_stats['live_users'],
                'favorites_count': favorites_count,
                'avg_interval': round(system_stats['avg_interval'], 1) if system_stats['avg_interval'] else 0,
                'min_interval': system_stats['min_interval'],
                'max_interval': system_stats['max_interval'],
                'total_recorded_lives': system_stats['total_recorded_lives'],
                'estimated_cycle_time': cycle_time_formatted,
                'estimated_cycle_seconds': estimated_cycle_seconds
            },
            'live_processes_capacity': {
                'active_processes': active_processes_count,
                'max_processes': max_live_processes,
                'capacity_percentage': round(capacity_percentage, 1),
                'capacity_status': capacity_status
            },
            'interval_distribution': interval_distribution,
            'config': {
                'min_user_interval': min_user_interval,
                'active_only_filter': active_only,
                'legacy_scheduler_stats_enabled': bool(
                    current_app.config.get('CONFIG', {})
                    .get('web_monitor', {})
                    .get('legacy_scheduler_stats_enabled', False)
                )
            },
            'last_updated': current_time.strftime('%Y-%m-%d %H:%M:%S')
        })

    except Exception as e:
        current_app.logger.error(f"Stats error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@stats_bp.route('/users/by-interval/<int:interval>', methods=['GET'])
def get_users_by_interval(interval):
    """Get users with specific check interval"""
    try:
        db = get_db()
        active_only = request.args.get('active_only', 'true').lower() == 'true'
        sort_by = request.args.get('sort_by', 'username')
        sort_order = request.args.get('sort_order', 'asc')

        # Base query conditions
        where_conditions = ["check_interval = ?"]
        params = [interval]

        if active_only:
            where_conditions.append("is_active = 1")

        where_clause = " AND ".join(where_conditions)

        # Validate sort parameters with whitelist
        valid_sort_fields = {
            'username': 'username',
            'check_interval': 'check_interval',
            'next_check': 'next_check',
            'total_lives': 'total_lives',
            'is_live': 'is_live',
            'is_active': 'is_active'
        }

        if sort_by not in valid_sort_fields:
            sort_by = 'username'
        safe_sort_column = valid_sort_fields[sort_by]

        # Validate sort order
        if sort_order not in ['asc', 'desc']:
            sort_order = 'asc'
        sort_direction = 'ASC' if sort_order == 'asc' else 'DESC'

        # Special handling for NULL next_check values
        if sort_by == 'next_check':
            order_clause = f"""
                ORDER BY
                    CASE WHEN {safe_sort_column} IS NULL THEN 0 ELSE 1 END {sort_direction},
                    {safe_sort_column} {sort_direction}
            """
        else:
            order_clause = f"ORDER BY {safe_sort_column} {sort_direction}"

        # Get users with specific interval
        users_query = f"""
            SELECT u.username, u.check_interval, u.is_live, u.is_active,
                   u.total_lives, u.next_check, u.added_at, u.is_favorite,
                   MIN(l.started_at) as live_started_at,
                   CASE
                       WHEN u.next_check IS NULL THEN 0
                       WHEN datetime(u.next_check) <= datetime('now', 'localtime') THEN
                           CAST((julianday('now', 'localtime') - julianday(u.next_check)) * 86400 AS INTEGER)
                       ELSE 0
                   END as delay_seconds
            FROM users u
            LEFT JOIN lives l ON u.id = l.user_id AND l.ended_at IS NULL
            WHERE {where_clause}
            GROUP BY u.username, u.check_interval, u.is_live, u.is_active,
                     u.total_lives, u.next_check, u.added_at, u.is_favorite
            {order_clause}
        """

        user_rows = db.execute(users_query, params).fetchall()
        users = user_data_transformer.transform_multiple_user_rows(user_rows, include_live_duration=True)

        return jsonify({
            'users': users,
            'interval': interval,
            'count': len(users),
            'active_only': active_only
        })

    except Exception as e:
        current_app.logger.error(f"Get users by interval error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

# Legacy API endpoints for compatibility
@stats_bp.route('/live_users', methods=['GET'])
def get_live_users():
    """Legacy endpoint - get currently live users"""
    try:
        db = get_db()
        cursor = db.execute("SELECT username FROM users WHERE is_live = 1 ORDER BY username")
        live_users_list = [row['username'] for row in cursor.fetchall()]
        return jsonify(live_users=live_users_list)
    except Exception as e:
        current_app.logger.error(f"Get live users error: {e}")
        return jsonify(error="Internal server error"), 500

@stats_bp.route('/update_priority', methods=['POST'])
@require_write_access
def update_user_priority():
    """Legacy endpoint - update user priority"""
    try:
        data = request.get_json()
        if not data or 'username' not in data or 'interval' not in data:
            return jsonify(success=False, message="Missing username or interval"), 400

        username = data['username']
        interval = int(data['interval'])

        db = get_db()
        success = update_user_properties(
            db,
            username,
            path_config=current_app.config['CONFIG'],
            check_interval=interval,
        )

        if success:
            return jsonify(success=True, message="Priority updated successfully")
        else:
            return jsonify(success=False, message="User not found"), 404

    except Exception as e:
        current_app.logger.error(f"Update priority error: {e}")
        return jsonify(success=False, message="Internal server error"), 500

@stats_bp.route('/test', methods=['GET'])
def test_endpoint():
    """Test endpoint"""
    return jsonify({'success': True, 'message': 'Test endpoint works'})
