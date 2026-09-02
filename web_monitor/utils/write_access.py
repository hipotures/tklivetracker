from functools import wraps

from flask import current_app, jsonify


def require_write_access(view):
    """Reject application mutations while the web monitor is read-only."""
    @wraps(view)
    def decorated_function(*args, **kwargs):
        if current_app.config.get("READ_ONLY", False):
            return jsonify({
                "error": "Write operations are not allowed in read-only mode",
                "read_only": True,
            }), 403
        return view(*args, **kwargs)

    return decorated_function
