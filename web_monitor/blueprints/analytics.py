from datetime import datetime, timedelta

from flask import Blueprint, current_app, jsonify, request

from web_monitor.utils.database import get_db


analytics_bp = Blueprint("analytics", __name__, url_prefix="/api")


VIEW_ALIASES = {
    "hourly": "day",
    "last24h": "day",
    "last7d": "7d",
    "daily": "30d",
    "last30d": "30d",
    "weekly": "3m",
    "last365d": "12m",
}
SUPPORTED_VIEWS = {"day", "7d", "30d", "3m", "12m"}

VIEW_AGGREGATIONS = {
    "day": ("hour", "Hour"),
    "7d": ("day", "Day"),
    "30d": ("day", "Day"),
    "3m": ("week", "Week"),
    "12m": ("month", "Month"),
}


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
    """Map legacy analytics view names to the current period model."""
    normalized = VIEW_ALIASES.get(value, value)
    return normalized if normalized in SUPPORTED_VIEWS else "30d"


def _parse_base_date(value, view, now):
    """Parse and clamp the requested base date to the local current date."""
    try:
        base_date = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        base_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if base_date > today:
        base_date = today

    if view in {"3m", "12m"}:
        base_date = base_date.replace(day=1)
    return base_date


def _period_bounds(view, base_date):
    """Return inclusive-start/exclusive-end bounds for an analytics period."""
    if view == "day":
        start = base_date
        end = start + timedelta(days=1)
    elif view == "7d":
        start = base_date - timedelta(days=6)
        end = base_date + timedelta(days=1)
    elif view == "30d":
        start = base_date - timedelta(days=29)
        end = base_date + timedelta(days=1)
    elif view == "3m":
        end_month = base_date.replace(day=1)
        start = _shift_months(end_month, -2)
        end = _shift_months(end_month, 1)
    elif view == "12m":
        end_month = base_date.replace(day=1)
        start = _shift_months(end_month, -11)
        end = _shift_months(end_month, 1)
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


def _bucket_definitions(view, start, end, now):
    """Build a continuous chart timeline, including explicit empty buckets."""
    aggregation, _ = VIEW_AGGREGATIONS[view]
    buckets = []

    if aggregation == "hour":
        cursor = start
        while cursor < end:
            buckets.append(
                {
                    "key": cursor.strftime("%Y-%m-%d %H:00:00"),
                    "time_period": cursor.strftime("%Y-%m-%d %H:00:00"),
                    "display_label": cursor.strftime("%H:00"),
                    "bucket_start": cursor,
                    "bucket_end": cursor + timedelta(hours=1),
                }
            )
            cursor += timedelta(hours=1)
    elif aggregation == "day":
        cursor = start
        while cursor < end:
            buckets.append(
                {
                    "key": cursor.strftime("%Y-%m-%d"),
                    "time_period": cursor.strftime("%Y-%m-%d"),
                    "display_label": cursor.strftime("%b %d"),
                    "bucket_start": cursor,
                    "bucket_end": cursor + timedelta(days=1),
                }
            )
            cursor += timedelta(days=1)
    elif aggregation == "week":
        cursor = start - timedelta(days=start.weekday())
        while cursor < end:
            buckets.append(
                {
                    "key": cursor.strftime("%Y-%m-%d"),
                    "time_period": cursor.strftime("%Y-%m-%d"),
                    "display_label": f"Week {cursor.strftime('%b %d')}",
                    "bucket_start": cursor,
                    "bucket_end": cursor + timedelta(days=7),
                }
            )
            cursor += timedelta(days=7)
    elif aggregation == "month":
        cursor = start.replace(day=1)
        while cursor < end:
            buckets.append(
                {
                    "key": cursor.strftime("%Y-%m"),
                    "time_period": cursor.strftime("%Y-%m"),
                    "display_label": cursor.strftime("%b %Y"),
                    "bucket_start": cursor,
                    "bucket_end": _shift_months(cursor, 1),
                }
            )
            cursor = _shift_months(cursor, 1)

    for bucket in buckets:
        bucket["is_future"] = bucket["bucket_start"] > now
        bucket["is_partial"] = (
            not bucket["is_future"]
            and (
                bucket["bucket_start"] < start
                or bucket["bucket_end"] > end
                or bucket["bucket_start"] <= now < bucket["bucket_end"]
            )
        )
        bucket.pop("bucket_start")
        bucket.pop("bucket_end")
    return buckets


def _period_range_label(view, start, end, now):
    """Create a human-readable range label that never implies future observations."""
    last_calendar_day = (end - timedelta(seconds=1)).date()
    visible_end = min(last_calendar_day, now.date())

    if view == "day":
        value = start
        return f"{value.strftime('%A, %B')} {value.day}, {value.year}"

    start_date = start.date()
    if start_date.year == visible_end.year:
        return (
            f"{start_date.strftime('%b')} {start_date.day} - "
            f"{visible_end.strftime('%b')} {visible_end.day}, {visible_end.year}"
        )
    return (
        f"{start_date.strftime('%b')} {start_date.day}, {start_date.year} - "
        f"{visible_end.strftime('%b')} {visible_end.day}, {visible_end.year}"
    )


def _navigation(view, base_date, start, end, now, db, table, timestamp_column):
    """Build period navigation without allowing forward navigation into the future."""
    if view == "day":
        previous_date = base_date - timedelta(days=1)
        next_date = base_date + timedelta(days=1)
    elif view == "7d":
        previous_date = base_date - timedelta(days=7)
        next_date = base_date + timedelta(days=7)
    elif view == "30d":
        previous_date = base_date - timedelta(days=30)
        next_date = base_date + timedelta(days=30)
    elif view == "3m":
        previous_date = _shift_months(base_date, -3)
        next_date = _shift_months(base_date, 3)
    elif view == "12m":
        previous_date = _shift_months(base_date, -12)
        next_date = _shift_months(base_date, 12)
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
        "current": base_date.strftime("%Y-%m-%d"),
        "has_prev": has_prev,
        "has_next": has_next,
    }


def _summary_from_series(data, value_key, aggregation_label):
    """Calculate summary values from observed buckets only."""
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


def _live_activity_payload(db, view, base_date, now):
    start, end = _period_bounds(view, base_date)
    query_end = min(end, now)
    aggregation, aggregation_label = VIEW_AGGREGATIONS[view]
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
        result = by_key[bucket["key"]]
        result["live_count"] = None if bucket["is_future"] else 0
        result["unique_users"] = None if bucket["is_future"] else 0
        data.append(result)

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

    summary = _summary_from_series(data, "live_count", aggregation_label)
    return {
        "success": True,
        "view_type": view,
        "date": base_date.strftime("%Y-%m-%d"),
        "data": data,
        "totals": {
            "total_live_sessions": totals["total_live_sessions"],
            "total_unique_users": totals["total_unique_users"],
        },
        "summary": summary,
        "period": {
            "aggregation": aggregation,
            "aggregation_label": aggregation_label,
            "start": start.strftime("%Y-%m-%d"),
            "end": min((end - timedelta(seconds=1)).date(), now.date()).strftime("%Y-%m-%d"),
            "range_label": _period_range_label(view, start, end, now),
            "is_current": end > now,
        },
        "navigation": _navigation(
            view, base_date, start, end, now, db, "lives", "started_at"
        ),
        "last_updated": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _new_users_payload(db, view, base_date, now):
    start, end = _period_bounds(view, base_date)
    query_end = min(end, now)
    aggregation, aggregation_label = VIEW_AGGREGATIONS[view]
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
        result = by_key[bucket["key"]]
        result["new_users_count"] = None if bucket["is_future"] else 0
        data.append(result)

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
        "date": base_date.strftime("%Y-%m-%d"),
        "data": data,
        "totals": {"total_new_users": total_new_users},
        "summary": _summary_from_series(data, "new_users_count", aggregation_label),
        "period": {
            "aggregation": aggregation,
            "aggregation_label": aggregation_label,
            "start": start.strftime("%Y-%m-%d"),
            "end": min((end - timedelta(seconds=1)).date(), now.date()).strftime("%Y-%m-%d"),
            "range_label": _period_range_label(view, start, end, now),
            "is_current": end > now,
        },
        "navigation": _navigation(
            view, base_date, start, end, now, db, "users", "added_at"
        ),
        "last_updated": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


@analytics_bp.route("/analytics/live-activity", methods=["GET"])
def get_live_activity_analytics():
    """Return live-start analytics using one consistent period model."""
    try:
        db = get_db()
        now = datetime.now()
        view = _normalize_view(request.args.get("view", "30d"))
        base_date = _parse_base_date(request.args.get("date"), view, now)
        return jsonify(_live_activity_payload(db, view, base_date, now))
    except Exception as exc:
        current_app.logger.error(f"Analytics error: {exc}")
        return jsonify({"success": False, "error": "Internal server error"}), 500


@analytics_bp.route("/analytics/new-users", methods=["GET"])
def get_new_users_analytics():
    """Return tracker-user additions using the same periods as live analytics."""
    try:
        db = get_db()
        now = datetime.now()
        view = _normalize_view(request.args.get("view", "30d"))
        base_date = _parse_base_date(request.args.get("date"), view, now)
        return jsonify(_new_users_payload(db, view, base_date, now))
    except Exception as exc:
        current_app.logger.error(f"New users analytics error: {exc}")
        return jsonify({"success": False, "error": "Internal server error"}), 500
