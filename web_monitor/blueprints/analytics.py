from datetime import datetime, timedelta

from flask import Blueprint, current_app, jsonify, request

from web_monitor.utils.database import get_db


analytics_bp = Blueprint("analytics", __name__, url_prefix="/api")


PERIODS = {
    "day": {
        "aggregation": "hour",
        "aggregation_label": "Hour",
    },
    "week": {
        "aggregation": "day",
        "aggregation_label": "Day",
    },
    "month": {
        "aggregation": "day",
        "aggregation_label": "Day",
    },
    "quarter": {
        "aggregation": "week",
        "aggregation_label": "Week",
    },
    "year": {
        "aggregation": "month",
        "aggregation_label": "Month",
    },
    "all": {
        "aggregation": "month",
        "aggregation_label": "Month",
    },
}

VIEW_ALIASES = {
    "hourly": "day",
    "last24h": "day",
    "7d": "week",
    "last7d": "week",
    "daily": "month",
    "30d": "month",
    "last30d": "month",
    "weekly": "quarter",
    "3m": "quarter",
    "12m": "year",
    "last365d": "year",
}
SUPPORTED_VIEWS = set(PERIODS)


def _shift_months(value, months):
    """Return the first day of the month shifted by the requested amount."""
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero_based = divmod(month_index, 12)
    return value.replace(
        year=year,
        month=month_zero_based + 1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def _normalize_view(value):
    """Map legacy analytics view names to the current calendar-period model."""
    normalized = VIEW_ALIASES.get(value, value)
    return normalized if normalized in SUPPORTED_VIEWS else "month"


def _parse_base_date(value, now):
    """Parse and clamp the requested reference date to the local current date."""
    try:
        base_date = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        base_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return min(base_date, today)


def _global_first_timestamp(db, now):
    """Return the earliest analytics timestamp across live starts and tracked users."""
    row = db.execute(
        """
        SELECT MIN(value) AS first_timestamp
        FROM (
            SELECT MIN(started_at) AS value FROM lives
            UNION ALL
            SELECT MIN(added_at) AS value FROM users
        )
        WHERE value IS NOT NULL
        """
    ).fetchone()
    value = row["first_timestamp"] if row is not None else None
    if not value:
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        current_app.logger.warning(
            "Could not parse earliest analytics timestamp %r; using current month",
            value,
        )
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _period_bounds(view, base_date, now, db):
    """Return inclusive-start/exclusive-end bounds for a calendar period."""
    if view == "day":
        start = base_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif view == "week":
        day = base_date.replace(hour=0, minute=0, second=0, microsecond=0)
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=7)
    elif view == "month":
        start = base_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = _shift_months(start, 1)
    elif view == "quarter":
        quarter_month = ((base_date.month - 1) // 3) * 3 + 1
        start = base_date.replace(
            month=quarter_month,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        end = _shift_months(start, 3)
    elif view == "year":
        start = base_date.replace(
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        end = start.replace(year=start.year + 1)
    elif view == "all":
        first_timestamp = _global_first_timestamp(db, now)
        start = first_timestamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now
    else:
        raise ValueError(f"Unsupported analytics view: {view}")
    return start, end


def _group_expression(timestamp_column, aggregation):
    """Return the SQLite expression used to group timestamps into chart buckets."""
    if aggregation == "hour":
        return f"strftime('%Y-%m-%d %H:00:00', {timestamp_column})"
    if aggregation == "day":
        return f"DATE({timestamp_column})"
    if aggregation == "week":
        return (
            f"date({timestamp_column}, '-' || "
            f"((CAST(strftime('%w', {timestamp_column}) AS INTEGER) + 6) % 7) "
            f"|| ' days')"
        )
    if aggregation == "month":
        return f"strftime('%Y-%m', {timestamp_column})"
    raise ValueError(f"Unsupported analytics aggregation: {aggregation}")


def _format_short_date(value):
    return f"{value.strftime('%b')} {value.day}"


def _format_date_range(start, end):
    """Format an inclusive date range without redundant year text."""
    if start == end:
        return f"{start.strftime('%b')} {start.day}, {start.year}"
    if start.year == end.year:
        return (
            f"{start.strftime('%b')} {start.day} - "
            f"{end.strftime('%b')} {end.day}, {end.year}"
        )
    return (
        f"{start.strftime('%b')} {start.day}, {start.year} - "
        f"{end.strftime('%b')} {end.day}, {end.year}"
    )


def _bucket_definitions(view, start, end, now):
    """Build the complete timeline for the selected calendar period."""
    aggregation = PERIODS[view]["aggregation"]
    buckets = []

    if aggregation == "hour":
        cursor = start
        while cursor < end:
            bucket_end = cursor + timedelta(hours=1)
            buckets.append(
                {
                    "key": cursor.strftime("%Y-%m-%d %H:00:00"),
                    "time_period": cursor.strftime("%Y-%m-%d %H:00:00"),
                    "display_label": cursor.strftime("%H:00"),
                    "bucket_start": cursor,
                    "bucket_end": bucket_end,
                }
            )
            cursor = bucket_end
    elif aggregation == "day":
        cursor = start
        while cursor < end:
            bucket_end = cursor + timedelta(days=1)
            buckets.append(
                {
                    "key": cursor.strftime("%Y-%m-%d"),
                    "time_period": cursor.strftime("%Y-%m-%d"),
                    "display_label": _format_short_date(cursor),
                    "bucket_start": cursor,
                    "bucket_end": bucket_end,
                }
            )
            cursor = bucket_end
    elif aggregation == "week":
        cursor = start - timedelta(days=start.weekday())
        while cursor < end:
            bucket_end = cursor + timedelta(days=7)
            visible_start = max(cursor, start)
            visible_end = min(bucket_end, end) - timedelta(days=1)
            buckets.append(
                {
                    "key": cursor.strftime("%Y-%m-%d"),
                    "time_period": cursor.strftime("%Y-%m-%d"),
                    "display_label": (
                        _format_short_date(visible_start)
                        if visible_start.date() == visible_end.date()
                        else f"{_format_short_date(visible_start)}–{_format_short_date(visible_end)}"
                    ),
                    "bucket_start": cursor,
                    "bucket_end": bucket_end,
                }
            )
            cursor = bucket_end
    elif aggregation == "month":
        cursor = start.replace(day=1)
        while cursor < end:
            bucket_end = _shift_months(cursor, 1)
            buckets.append(
                {
                    "key": cursor.strftime("%Y-%m"),
                    "time_period": cursor.strftime("%Y-%m"),
                    "display_label": cursor.strftime("%b %Y"),
                    "bucket_start": cursor,
                    "bucket_end": bucket_end,
                }
            )
            cursor = bucket_end

    for bucket in buckets:
        effective_start = max(bucket["bucket_start"], start)
        effective_end = min(bucket["bucket_end"], end)
        bucket["is_future"] = effective_start > now
        bucket["is_partial"] = (
            not bucket["is_future"]
            and (
                bucket["bucket_start"] < start
                or bucket["bucket_end"] > end
                or effective_start <= now < effective_end
            )
        )
        bucket.pop("bucket_start")
        bucket.pop("bucket_end")
    return buckets


def _period_label(view, start, end, now):
    """Return an unambiguous label for the complete selected period."""
    if view == "day":
        return f"{start.strftime('%A, %B')} {start.day}, {start.year}"
    if view == "week":
        return _format_date_range(start.date(), (end - timedelta(days=1)).date())
    if view == "month":
        return start.strftime("%B %Y")
    if view == "quarter":
        quarter = ((start.month - 1) // 3) + 1
        return (
            f"Q{quarter} {start.year} · "
            f"{_format_date_range(start.date(), (end - timedelta(days=1)).date())}"
        )
    if view == "year":
        return str(start.year)
    if view == "all":
        first = start.date()
        last = now.date()
        return f"All time · {_format_date_range(first, last)}"
    raise ValueError(f"Unsupported analytics view: {view}")


def _navigation(view, start, end, now, db, table, timestamp_column):
    """Build navigation between complete adjacent calendar periods."""
    if view == "all":
        return {
            "prev": None,
            "next": None,
            "current": None,
            "has_prev": False,
            "has_next": False,
        }

    if view == "day":
        previous_date = start - timedelta(days=1)
        next_date = start + timedelta(days=1)
    elif view == "week":
        previous_date = start - timedelta(days=7)
        next_date = start + timedelta(days=7)
    elif view == "month":
        previous_date = _shift_months(start, -1)
        next_date = _shift_months(start, 1)
    elif view == "quarter":
        previous_date = _shift_months(start, -3)
        next_date = _shift_months(start, 3)
    elif view == "year":
        previous_date = start.replace(year=start.year - 1)
        next_date = start.replace(year=start.year + 1)
    else:
        raise ValueError(f"Unsupported analytics view: {view}")

    has_prev = (
        db.execute(
            f"SELECT 1 FROM {table} WHERE {timestamp_column} < ? LIMIT 1",
            (start.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchone()
        is not None
    )
    has_next = end <= now

    return {
        "prev": previous_date.strftime("%Y-%m-%d"),
        "next": next_date.strftime("%Y-%m-%d"),
        "current": start.strftime("%Y-%m-%d"),
        "has_prev": has_prev,
        "has_next": has_next,
    }


def _summary_from_series(data, value_key, aggregation_label):
    """Calculate summary values from completed chart buckets only."""
    observed = [item for item in data if item[value_key] is not None]
    completed = [item for item in observed if not item.get("is_partial")]
    completed_total = sum(item[value_key] for item in completed)
    average = completed_total / len(completed) if completed else 0

    peak = None
    positive = [item for item in observed if item[value_key] > 0]
    if positive:
        peak_item = max(positive, key=lambda item: item[value_key])
        peak = {
            "display_label": peak_item["display_label"],
            "count": peak_item[value_key],
        }

    return {
        "observed_periods": len(observed),
        "average_periods": len(completed),
        "average_per_period": round(average, 1),
        "aggregation_label": aggregation_label,
        "peak": peak,
    }


def _period_metadata(view, start, end, now):
    inclusive_end = now.date() if view == "all" else (end - timedelta(seconds=1)).date()
    return {
        "name": view,
        "aggregation": PERIODS[view]["aggregation"],
        "aggregation_label": PERIODS[view]["aggregation_label"],
        "start": start.strftime("%Y-%m-%d"),
        "end": inclusive_end.strftime("%Y-%m-%d"),
        "range_label": _period_label(view, start, end, now),
        "is_current": view == "all" or start <= now < end,
    }


def _live_activity_payload(db, view, base_date, now):
    start, end = _period_bounds(view, base_date, now, db)
    query_end = min(end, now)
    aggregation = PERIODS[view]["aggregation"]
    aggregation_label = PERIODS[view]["aggregation_label"]
    group_expression = _group_expression("started_at", aggregation)
    buckets = _bucket_definitions(view, start, end, now)
    by_key = {item["key"]: item for item in buckets}

    results = db.execute(
        f"""
        SELECT
            {group_expression} AS bucket_key,
            COUNT(*) AS live_count,
            COUNT(DISTINCT user_id) AS unique_users
        FROM lives
        WHERE started_at >= ? AND started_at < ?
        GROUP BY {group_expression}
        ORDER BY bucket_key
        """,
        (
            start.strftime("%Y-%m-%d %H:%M:%S"),
            query_end.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    ).fetchall()

    data = []
    for bucket in buckets:
        item = by_key[bucket["key"]]
        item["live_count"] = None if bucket["is_future"] else 0
        item["unique_users"] = None if bucket["is_future"] else 0
        data.append(item)

    for row in results:
        item = by_key.get(row["bucket_key"])
        if item is not None and not item["is_future"]:
            item["live_count"] = row["live_count"]
            item["unique_users"] = row["unique_users"]

    totals = db.execute(
        """
        SELECT
            COUNT(*) AS total_live_sessions,
            COUNT(DISTINCT user_id) AS total_unique_users
        FROM lives
        WHERE started_at >= ? AND started_at < ?
        """,
        (
            start.strftime("%Y-%m-%d %H:%M:%S"),
            query_end.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    ).fetchone()

    return {
        "success": True,
        "view_type": view,
        "date": start.strftime("%Y-%m-%d") if view != "all" else None,
        "data": data,
        "totals": {
            "total_live_sessions": totals["total_live_sessions"],
            "total_unique_users": totals["total_unique_users"],
        },
        "summary": _summary_from_series(data, "live_count", aggregation_label),
        "period": _period_metadata(view, start, end, now),
        "navigation": _navigation(
            view, start, end, now, db, "lives", "started_at"
        ),
        "last_updated": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _new_users_payload(db, view, base_date, now):
    start, end = _period_bounds(view, base_date, now, db)
    query_end = min(end, now)
    aggregation = PERIODS[view]["aggregation"]
    aggregation_label = PERIODS[view]["aggregation_label"]
    group_expression = _group_expression("added_at", aggregation)
    buckets = _bucket_definitions(view, start, end, now)
    by_key = {item["key"]: item for item in buckets}

    results = db.execute(
        f"""
        SELECT
            {group_expression} AS bucket_key,
            COUNT(*) AS new_users_count
        FROM users
        WHERE added_at >= ? AND added_at < ?
        GROUP BY {group_expression}
        ORDER BY bucket_key
        """,
        (
            start.strftime("%Y-%m-%d %H:%M:%S"),
            query_end.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    ).fetchall()

    data = []
    for bucket in buckets:
        item = by_key[bucket["key"]]
        item["new_users_count"] = None if bucket["is_future"] else 0
        data.append(item)

    for row in results:
        item = by_key.get(row["bucket_key"])
        if item is not None and not item["is_future"]:
            item["new_users_count"] = row["new_users_count"]

    total_new_users = db.execute(
        """
        SELECT COUNT(*) AS total_new_users
        FROM users
        WHERE added_at >= ? AND added_at < ?
        """,
        (
            start.strftime("%Y-%m-%d %H:%M:%S"),
            query_end.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    ).fetchone()["total_new_users"]

    return {
        "success": True,
        "view_type": view,
        "date": start.strftime("%Y-%m-%d") if view != "all" else None,
        "data": data,
        "totals": {"total_new_users": total_new_users},
        "summary": _summary_from_series(data, "new_users_count", aggregation_label),
        "period": _period_metadata(view, start, end, now),
        "navigation": _navigation(
            view, start, end, now, db, "users", "added_at"
        ),
        "last_updated": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


@analytics_bp.route("/analytics/live-activity", methods=["GET"])
def get_live_activity_analytics():
    """Return live-start analytics for a complete calendar period."""
    try:
        db = get_db()
        now = datetime.now()
        view = _normalize_view(request.args.get("view", "month"))
        base_date = _parse_base_date(request.args.get("date"), now)
        return jsonify(_live_activity_payload(db, view, base_date, now))
    except Exception as exc:
        current_app.logger.error(f"Analytics error: {exc}")
        return jsonify({"success": False, "error": "Internal server error"}), 500


@analytics_bp.route("/analytics/new-users", methods=["GET"])
def get_new_users_analytics():
    """Return tracked-user additions for the same calendar periods."""
    try:
        db = get_db()
        now = datetime.now()
        view = _normalize_view(request.args.get("view", "month"))
        base_date = _parse_base_date(request.args.get("date"), now)
        return jsonify(_new_users_payload(db, view, base_date, now))
    except Exception as exc:
        current_app.logger.error(f"New users analytics error: {exc}")
        return jsonify({"success": False, "error": "Internal server error"}), 500
