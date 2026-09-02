from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, current_app

# Import utilities
from web_monitor.utils.database import get_db

# Create blueprint
analytics_bp = Blueprint('analytics', __name__, url_prefix='/api')

@analytics_bp.route('/analytics/live-activity', methods=['GET'])
def get_live_activity_analytics():
    """Get live activity analytics data with hourly/daily/weekly aggregation"""
    try:
        db = get_db()

        # Get parameters
        view_type = request.args.get('view', 'daily')  # hourly, daily, weekly
        date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))

        # Validate view type
        if view_type not in [
            'hourly', 'daily', 'weekly', 'last24h', 'last7d', 'last30d',
            'last365d'
        ]:
            view_type = 'daily'

        # Parse date
        try:
            base_date = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            base_date = datetime.now()

        analytics_data = []

        if view_type == 'hourly':
            # Hourly view: Live sessions per hour for selected day
            start_date = base_date.replace(hour=0, minute=0, second=0, microsecond=0)
            end_date = start_date + timedelta(days=1)

            query = """
                SELECT
                    strftime('%Y-%m-%d %H:00:00', started_at) as time_period,
                    COUNT(*) as live_count,
                    COUNT(DISTINCT user_id) as unique_users
                FROM lives
                WHERE started_at >= ? AND started_at < ?
                GROUP BY strftime('%Y-%m-%d %H:00:00', started_at)
                ORDER BY time_period
            """

            # Create full 24-hour structure
            for hour in range(24):
                hour_time = start_date.replace(hour=hour)
                analytics_data.append({
                    'time_period': hour_time.strftime('%Y-%m-%d %H:00:00'),
                    'live_count': 0,
                    'unique_users': 0,
                    'display_label': hour_time.strftime('%H:00')
                })

            # Execute query and merge results
            results = db.execute(query, (start_date.strftime('%Y-%m-%d %H:%M:%S'),
                                       end_date.strftime('%Y-%m-%d %H:%M:%S'))).fetchall()

            # Update analytics_data with actual results
            for row in results:
                hour_key = row['time_period']
                for item in analytics_data:
                    if item['time_period'] == hour_key:
                        item['live_count'] = row['live_count']
                        item['unique_users'] = row['unique_users']
                        break

        elif view_type == 'daily':
            # Daily view: Live sessions per day for 30 days ending on selected date
            end_date = base_date + timedelta(days=1)  # Include selected date
            start_date = base_date - timedelta(days=29)  # 30 days total

            query = """
                SELECT
                    DATE(started_at) as time_period,
                    COUNT(*) as live_count,
                    COUNT(DISTINCT user_id) as unique_users
                FROM lives
                WHERE started_at >= ? AND started_at < ?
                GROUP BY DATE(started_at)
                ORDER BY time_period
            """

            # Create full 30-day structure
            for day in range(30):
                day_date = start_date + timedelta(days=day)
                analytics_data.append({
                    'time_period': day_date.strftime('%Y-%m-%d'),
                    'live_count': 0,
                    'unique_users': 0,
                    'display_label': day_date.strftime('%m/%d')
                })

            # Execute query and merge results
            results = db.execute(query, (start_date.strftime('%Y-%m-%d 00:00:00'),
                                       end_date.strftime('%Y-%m-%d 00:00:00'))).fetchall()

            # Update analytics_data with actual results
            for row in results:
                day_key = row['time_period']
                for item in analytics_data:
                    if item['time_period'] == day_key:
                        item['live_count'] = row['live_count']
                        item['unique_users'] = row['unique_users']
                        break

        elif view_type == 'weekly':
            # Weekly view: Live sessions per week for quarter (3 months) ending in selected month
            # Get first day of selected month
            end_month_start = base_date.replace(day=1)
            # Get first day of next month
            if end_month_start.month == 12:
                end_date = end_month_start.replace(year=end_month_start.year + 1, month=1)
            else:
                end_date = end_month_start.replace(month=end_month_start.month + 1)

            # Go back 3 months for quarter view
            start_month = end_month_start.month - 2
            start_year = end_month_start.year
            if start_month <= 0:
                start_month += 12
                start_year -= 1
            start_date = end_month_start.replace(year=start_year, month=start_month, day=1)

            query = """
                SELECT
                    strftime('%Y-W%W', started_at) as week_key,
                    MIN(DATE(started_at)) as week_start,
                    MAX(DATE(started_at)) as week_end,
                    COUNT(*) as live_count,
                    COUNT(DISTINCT user_id) as unique_users
                FROM lives
                WHERE started_at >= ? AND started_at < ?
                GROUP BY strftime('%Y-W%W', started_at)
                ORDER BY week_start
            """

            results = db.execute(query, (start_date.strftime('%Y-%m-%d 00:00:00'),
                                       end_date.strftime('%Y-%m-%d 00:00:00'))).fetchall()

            for row in results:
                week_start = datetime.strptime(row['week_start'], '%Y-%m-%d')
                analytics_data.append({
                    'time_period': row['week_key'],
                    'live_count': row['live_count'],
                    'unique_users': row['unique_users'],
                    'display_label': f"Week {week_start.strftime('%m/%d')}"
                })

        elif view_type == 'last24h':
            # Last 24 hours: Live sessions per hour from now backwards
            end_date = datetime.now()
            start_date = end_date - timedelta(hours=24)

            query = """
                SELECT
                    strftime('%Y-%m-%d %H:00:00', started_at) as time_period,
                    COUNT(*) as live_count,
                    COUNT(DISTINCT user_id) as unique_users
                FROM lives
                WHERE started_at >= ? AND started_at < ?
                GROUP BY strftime('%Y-%m-%d %H:00:00', started_at)
                ORDER BY time_period
            """

            # Create full 24-hour structure
            for i in range(24):
                hour_time = start_date + timedelta(hours=i)
                analytics_data.append({
                    'time_period': hour_time.strftime('%Y-%m-%d %H:00:00'),
                    'live_count': 0,
                    'unique_users': 0,
                    'display_label': hour_time.strftime('%H:00')
                })

            # Execute query and merge results
            results = db.execute(query, (start_date.strftime('%Y-%m-%d %H:%M:%S'),
                                       end_date.strftime('%Y-%m-%d %H:%M:%S'))).fetchall()

            # Update analytics_data with actual results
            for row in results:
                hour_key = row['time_period']
                for item in analytics_data:
                    if item['time_period'] == hour_key:
                        item['live_count'] = row['live_count']
                        item['unique_users'] = row['unique_users']
                        break

        elif view_type == 'last7d':
            # Last 7 days: Live sessions per day from now backwards
            end_date = datetime.now()
            start_date = end_date - timedelta(days=7)

            query = """
                SELECT
                    DATE(started_at) as time_period,
                    COUNT(*) as live_count,
                    COUNT(DISTINCT user_id) as unique_users
                FROM lives
                WHERE started_at >= ? AND started_at < ?
                GROUP BY DATE(started_at)
                ORDER BY time_period
            """

            # Create full 7-day structure
            for i in range(7):
                day_date = start_date + timedelta(days=i)
                analytics_data.append({
                    'time_period': day_date.strftime('%Y-%m-%d'),
                    'live_count': 0,
                    'unique_users': 0,
                    'display_label': day_date.strftime('%m/%d')
                })

            # Execute query and merge results
            results = db.execute(query, (start_date.strftime('%Y-%m-%d 00:00:00'),
                                       end_date.strftime('%Y-%m-%d 00:00:00'))).fetchall()

            # Update analytics_data with actual results
            for row in results:
                day_key = row['time_period']
                for item in analytics_data:
                    if item['time_period'] == day_key:
                        item['live_count'] = row['live_count']
                        item['unique_users'] = row['unique_users']
                        break

        elif view_type in ['last30d', 'last365d']:
            # Recent live sessions grouped by day.
            day_count = 365 if view_type == 'last365d' else 30
            end_date = datetime.now()
            start_date = end_date - timedelta(days=day_count)

            query = """
                SELECT
                    DATE(started_at) as time_period,
                    COUNT(*) as live_count,
                    COUNT(DISTINCT user_id) as unique_users
                FROM lives
                WHERE started_at >= ? AND started_at < ?
                GROUP BY DATE(started_at)
                ORDER BY time_period
            """

            # Include empty days so the chart keeps a continuous timeline.
            for i in range(day_count):
                day_date = start_date + timedelta(days=i)
                analytics_data.append({
                    'time_period': day_date.strftime('%Y-%m-%d'),
                    'live_count': 0,
                    'unique_users': 0,
                    'display_label': day_date.strftime('%m/%d')
                })

            # Execute query and merge results
            results = db.execute(query, (start_date.strftime('%Y-%m-%d 00:00:00'),
                                       end_date.strftime('%Y-%m-%d 00:00:00'))).fetchall()

            # Update analytics_data with actual results
            for row in results:
                day_key = row['time_period']
                for item in analytics_data:
                    if item['time_period'] == day_key:
                        item['live_count'] = row['live_count']
                        item['unique_users'] = row['unique_users']
                        break

        # Calculate totals
        total_live_count = sum(item['live_count'] for item in analytics_data)
        total_unique_users = len(set(item['unique_users'] for item in analytics_data if item['unique_users'] > 0))

        # Calculate navigation dates
        nav_dates = {}
        if view_type == 'hourly':
            nav_dates['prev'] = (base_date - timedelta(days=1)).strftime('%Y-%m-%d')
            nav_dates['next'] = (base_date + timedelta(days=1)).strftime('%Y-%m-%d')
            nav_dates['current'] = base_date.strftime('%Y-%m-%d')
        elif view_type == 'daily':
            # 30-day periods
            nav_dates['prev'] = (base_date - timedelta(days=30)).strftime('%Y-%m-%d')
            nav_dates['next'] = (base_date + timedelta(days=30)).strftime('%Y-%m-%d')
            nav_dates['current'] = base_date.strftime('%Y-%m-%d')
        elif view_type == 'weekly':
            # 3-month (quarter) periods
            # Previous quarter
            prev_month = base_date.month - 3
            prev_year = base_date.year
            if prev_month <= 0:
                prev_month += 12
                prev_year -= 1
            prev_quarter = base_date.replace(
                year=prev_year, month=prev_month, day=1
            )

            # Next quarter
            next_month = base_date.month + 3
            next_year = base_date.year
            if next_month > 12:
                next_month -= 12
                next_year += 1
            next_quarter = base_date.replace(
                year=next_year, month=next_month, day=1
            )

            nav_dates['prev'] = prev_quarter.strftime('%Y-%m-%d')
            nav_dates['next'] = next_quarter.strftime('%Y-%m-%d')
            nav_dates['current'] = base_date.strftime('%Y-%m-%d')
        elif view_type in ['last24h', 'last7d', 'last30d', 'last365d']:
            # For "last" views, navigation is disabled - always shows most recent data
            nav_dates['prev'] = datetime.now().strftime('%Y-%m-%d')
            nav_dates['next'] = datetime.now().strftime('%Y-%m-%d')
            nav_dates['current'] = datetime.now().strftime('%Y-%m-%d')

        # Check data availability for navigation
        if view_type in ['last24h', 'last7d', 'last30d', 'last365d']:
            # For "last" views, navigation is always disabled
            has_prev_data = False
            has_next_data = False
        else:
            # Check if there's data before the current period
            prev_check_query = "SELECT COUNT(*) as count FROM lives WHERE started_at < ?"
            has_prev_data = db.execute(prev_check_query, (start_date.strftime('%Y-%m-%d 00:00:00'),)).fetchone()['count'] > 0

            # Check if there's data after the current period (also don't go into future)
            next_check_query = "SELECT COUNT(*) as count FROM lives WHERE started_at >= ?"
            today = datetime.now().strftime('%Y-%m-%d 23:59:59')
            has_next_data = (db.execute(next_check_query, (end_date.strftime('%Y-%m-%d 00:00:00'),)).fetchone()['count'] > 0
                            and nav_dates['next'] <= today)

        return jsonify({
            'success': True,
            'view_type': view_type,
            'date': date_str,
            'data': analytics_data,
            'totals': {
                'total_live_sessions': total_live_count,
                'total_unique_users': total_unique_users
            },
            'navigation': {
                **nav_dates,
                'has_prev': has_prev_data,
                'has_next': has_next_data
            },
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })

    except Exception as e:
        current_app.logger.error(f"Analytics error: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

@analytics_bp.route('/analytics/new-users', methods=['GET'])
def get_new_users_analytics():
    """Get new users analytics data with hourly/daily/weekly aggregation"""
    try:
        db = get_db()

        # Get parameters
        view_type = request.args.get('view', 'daily')  # hourly, daily, weekly
        date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))

        # Validate view type
        if view_type not in [
            'hourly', 'daily', 'weekly', 'last24h', 'last7d', 'last30d',
            'last365d'
        ]:
            view_type = 'daily'

        # Parse date
        try:
            base_date = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            base_date = datetime.now()

        analytics_data = []

        if view_type == 'hourly':
            # Hourly view: New users per hour for selected day
            start_date = base_date.replace(hour=0, minute=0, second=0, microsecond=0)
            end_date = start_date + timedelta(days=1)

            query = """
                SELECT
                    strftime('%Y-%m-%d %H:00:00', added_at) as time_period,
                    COUNT(*) as new_users_count
                FROM users
                WHERE added_at >= ? AND added_at < ?
                GROUP BY strftime('%Y-%m-%d %H:00:00', added_at)
                ORDER BY time_period
            """

            # Create full 24-hour structure
            for hour in range(24):
                hour_time = start_date.replace(hour=hour)
                analytics_data.append({
                    'time_period': hour_time.strftime('%Y-%m-%d %H:00:00'),
                    'new_users_count': 0,
                    'display_label': hour_time.strftime('%H:00')
                })

            # Execute query and merge results
            results = db.execute(query, (start_date.strftime('%Y-%m-%d %H:%M:%S'),
                                       end_date.strftime('%Y-%m-%d %H:%M:%S'))).fetchall()

            # Update analytics_data with actual results
            for row in results:
                hour_key = row['time_period']
                for item in analytics_data:
                    if item['time_period'] == hour_key:
                        item['new_users_count'] = row['new_users_count']
                        break

        elif view_type == 'daily':
            # Daily view: New users per day for 30 days ending on selected date
            end_date = base_date + timedelta(days=1)  # Include selected date
            start_date = base_date - timedelta(days=29)  # 30 days total

            query = """
                SELECT
                    DATE(added_at) as time_period,
                    COUNT(*) as new_users_count
                FROM users
                WHERE added_at >= ? AND added_at < ?
                GROUP BY DATE(added_at)
                ORDER BY time_period
            """

            # Create full 30-day structure
            for day in range(30):
                day_date = start_date + timedelta(days=day)
                analytics_data.append({
                    'time_period': day_date.strftime('%Y-%m-%d'),
                    'new_users_count': 0,
                    'display_label': day_date.strftime('%m/%d')
                })

            # Execute query and merge results
            results = db.execute(query, (start_date.strftime('%Y-%m-%d 00:00:00'),
                                       end_date.strftime('%Y-%m-%d 00:00:00'))).fetchall()

            # Update analytics_data with actual results
            for row in results:
                day_key = row['time_period']
                for item in analytics_data:
                    if item['time_period'] == day_key:
                        item['new_users_count'] = row['new_users_count']
                        break

        elif view_type == 'weekly':
            # Weekly view: New users per week for quarter (3 months) ending in selected month
            # Get first day of selected month
            end_month_start = base_date.replace(day=1)
            # Get first day of next month
            if end_month_start.month == 12:
                end_date = end_month_start.replace(year=end_month_start.year + 1, month=1)
            else:
                end_date = end_month_start.replace(month=end_month_start.month + 1)

            # Go back 3 months for quarter view
            start_month = end_month_start.month - 2
            start_year = end_month_start.year
            if start_month <= 0:
                start_month += 12
                start_year -= 1
            start_date = end_month_start.replace(year=start_year, month=start_month, day=1)

            query = """
                SELECT
                    strftime('%Y-W%W', added_at) as week_key,
                    MIN(DATE(added_at)) as week_start,
                    MAX(DATE(added_at)) as week_end,
                    COUNT(*) as new_users_count
                FROM users
                WHERE added_at >= ? AND added_at < ?
                GROUP BY strftime('%Y-W%W', added_at)
                ORDER BY week_start
            """

            results = db.execute(query, (start_date.strftime('%Y-%m-%d 00:00:00'),
                                       end_date.strftime('%Y-%m-%d 00:00:00'))).fetchall()

            for row in results:
                week_start = datetime.strptime(row['week_start'], '%Y-%m-%d')
                analytics_data.append({
                    'time_period': row['week_key'],
                    'new_users_count': row['new_users_count'],
                    'display_label': f"Week {week_start.strftime('%m/%d')}"
                })

        elif view_type == 'last24h':
            # Last 24 hours: New users per hour from now backwards
            end_date = datetime.now()
            start_date = end_date - timedelta(hours=24)

            query = """
                SELECT
                    strftime('%Y-%m-%d %H:00:00', added_at) as time_period,
                    COUNT(*) as new_users_count
                FROM users
                WHERE added_at >= ? AND added_at < ?
                GROUP BY strftime('%Y-%m-%d %H:00:00', added_at)
                ORDER BY time_period
            """

            # Create full 24-hour structure
            for i in range(24):
                hour_time = start_date + timedelta(hours=i)
                analytics_data.append({
                    'time_period': hour_time.strftime('%Y-%m-%d %H:00:00'),
                    'new_users_count': 0,
                    'display_label': hour_time.strftime('%H:00')
                })

            # Execute query and merge results
            results = db.execute(query, (start_date.strftime('%Y-%m-%d %H:%M:%S'),
                                       end_date.strftime('%Y-%m-%d %H:%M:%S'))).fetchall()

            # Update analytics_data with actual results
            for row in results:
                hour_key = row['time_period']
                for item in analytics_data:
                    if item['time_period'] == hour_key:
                        item['new_users_count'] = row['new_users_count']
                        break

        elif view_type == 'last7d':
            # Last 7 days: New users per day from now backwards
            end_date = datetime.now()
            start_date = end_date - timedelta(days=7)

            query = """
                SELECT
                    DATE(added_at) as time_period,
                    COUNT(*) as new_users_count
                FROM users
                WHERE added_at >= ? AND added_at < ?
                GROUP BY DATE(added_at)
                ORDER BY time_period
            """

            # Create full 7-day structure
            for i in range(7):
                day_date = start_date + timedelta(days=i)
                analytics_data.append({
                    'time_period': day_date.strftime('%Y-%m-%d'),
                    'new_users_count': 0,
                    'display_label': day_date.strftime('%m/%d')
                })

            # Execute query and merge results
            results = db.execute(query, (start_date.strftime('%Y-%m-%d 00:00:00'),
                                       end_date.strftime('%Y-%m-%d 00:00:00'))).fetchall()

            # Update analytics_data with actual results
            for row in results:
                day_key = row['time_period']
                for item in analytics_data:
                    if item['time_period'] == day_key:
                        item['new_users_count'] = row['new_users_count']
                        break

        elif view_type in ['last30d', 'last365d']:
            # Recently added users grouped by day.
            day_count = 365 if view_type == 'last365d' else 30
            end_date = datetime.now()
            start_date = end_date - timedelta(days=day_count)

            query = """
                SELECT
                    DATE(added_at) as time_period,
                    COUNT(*) as new_users_count
                FROM users
                WHERE added_at >= ? AND added_at < ?
                GROUP BY DATE(added_at)
                ORDER BY time_period
            """

            # Include empty days so the chart keeps a continuous timeline.
            for i in range(day_count):
                day_date = start_date + timedelta(days=i)
                analytics_data.append({
                    'time_period': day_date.strftime('%Y-%m-%d'),
                    'new_users_count': 0,
                    'display_label': day_date.strftime('%m/%d')
                })

            # Execute query and merge results
            results = db.execute(query, (start_date.strftime('%Y-%m-%d 00:00:00'),
                                       end_date.strftime('%Y-%m-%d 00:00:00'))).fetchall()

            # Update analytics_data with actual results
            for row in results:
                day_key = row['time_period']
                for item in analytics_data:
                    if item['time_period'] == day_key:
                        item['new_users_count'] = row['new_users_count']
                        break

        # Calculate totals
        total_new_users = sum(item['new_users_count'] for item in analytics_data)

        # Calculate navigation dates
        nav_dates = {}
        if view_type == 'hourly':
            nav_dates['prev'] = (base_date - timedelta(days=1)).strftime('%Y-%m-%d')
            nav_dates['next'] = (base_date + timedelta(days=1)).strftime('%Y-%m-%d')
            nav_dates['current'] = base_date.strftime('%Y-%m-%d')
        elif view_type == 'daily':
            # 30-day periods
            nav_dates['prev'] = (base_date - timedelta(days=30)).strftime('%Y-%m-%d')
            nav_dates['next'] = (base_date + timedelta(days=30)).strftime('%Y-%m-%d')
            nav_dates['current'] = base_date.strftime('%Y-%m-%d')
        elif view_type == 'weekly':
            # 3-month (quarter) periods
            # Previous quarter
            prev_month = base_date.month - 3
            prev_year = base_date.year
            if prev_month <= 0:
                prev_month += 12
                prev_year -= 1
            prev_quarter = base_date.replace(
                year=prev_year, month=prev_month, day=1
            )

            # Next quarter
            next_month = base_date.month + 3
            next_year = base_date.year
            if next_month > 12:
                next_month -= 12
                next_year += 1
            next_quarter = base_date.replace(
                year=next_year, month=next_month, day=1
            )

            nav_dates['prev'] = prev_quarter.strftime('%Y-%m-%d')
            nav_dates['next'] = next_quarter.strftime('%Y-%m-%d')
            nav_dates['current'] = base_date.strftime('%Y-%m-%d')
        elif view_type in ['last24h', 'last7d', 'last30d', 'last365d']:
            # For "last" views, navigation is disabled - always shows most recent data
            nav_dates['prev'] = datetime.now().strftime('%Y-%m-%d')
            nav_dates['next'] = datetime.now().strftime('%Y-%m-%d')
            nav_dates['current'] = datetime.now().strftime('%Y-%m-%d')

        # Check data availability for navigation
        if view_type in ['last24h', 'last7d', 'last30d', 'last365d']:
            # For "last" views, navigation is always disabled
            has_prev_data = False
            has_next_data = False
        else:
            # Check if there's data before the current period
            prev_check_query = "SELECT COUNT(*) as count FROM users WHERE added_at < ?"
            has_prev_data = db.execute(prev_check_query, (start_date.strftime('%Y-%m-%d 00:00:00'),)).fetchone()['count'] > 0

            # Check if there's data after the current period (also don't go into future)
            next_check_query = "SELECT COUNT(*) as count FROM users WHERE added_at >= ?"
            today = datetime.now().strftime('%Y-%m-%d 23:59:59')
            has_next_data = (db.execute(next_check_query, (end_date.strftime('%Y-%m-%d 00:00:00'),)).fetchone()['count'] > 0
                            and nav_dates['next'] <= today)

        return jsonify({
            'success': True,
            'view_type': view_type,
            'date': date_str,
            'data': analytics_data,
            'totals': {
                'total_new_users': total_new_users
            },
            'navigation': {
                **nav_dates,
                'has_prev': has_prev_data,
                'has_next': has_next_data
            },
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })

    except Exception as e:
        current_app.logger.error(f"New users analytics error: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500
