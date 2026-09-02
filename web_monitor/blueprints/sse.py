from flask import Blueprint, Response, request, jsonify, current_app

# Import services
from web_monitor.services.notification_service import notification_service
from web_monitor.utils.validation import sanitize_username
from web_monitor.utils.write_access import require_write_access

# Create blueprint
sse_bp = Blueprint('sse', __name__, url_prefix='/api')

@sse_bp.route('/events')
def events():
    """Server-Sent Events endpoint for real-time notifications"""
    return Response(
        notification_service.create_event_stream(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive'
        }
    )

@sse_bp.route('/events/room-id-unavailable', methods=['POST'])
@require_write_access
def room_id_unavailable_notification():
    """Notify connected dashboards that a live user could not start recording."""
    data = request.get_json(silent=True) or {}
    username = sanitize_username(data.get('username'))
    if username is None:
        return jsonify({'success': False, 'error': 'Invalid username'}), 400

    message = f'{username} is LIVE, but recording could not start because RoomID is unavailable.'
    notification_service.send_notification_event(
        'recorder_room_id_unavailable',
        username,
        status='warning',
        message=message,
    )

    return jsonify({
        'success': True,
        'active_clients': len(notification_service.active_sse_clients),
    })

@sse_bp.route('/test-notification', methods=['POST'])
@require_write_access
def test_notification():
    """Test endpoint for sending SSE notifications"""
    try:
        data = request.get_json() or {}
        username = sanitize_username(data.get('username', 'test_user'))
        if username is None:
            return jsonify({'success': False, 'error': 'Invalid username'}), 400
        event_type = data.get('type', 'live_start')
        message = data.get('message', f'{username} is now live!')

        notification_service.send_notification_event(event_type, username, message=message)

        return jsonify({
            'success': True,
            'message': f'Test notification sent for {username}',
            'active_clients': len(notification_service.active_sse_clients)
        })
    except Exception as e:
        current_app.logger.error(f"Test notification error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
