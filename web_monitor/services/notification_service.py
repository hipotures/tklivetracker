import queue
import threading
import json
from datetime import datetime
from flask import current_app

class NotificationService:
    def __init__(self):
        self.active_sse_clients = []
        self.sse_lock = threading.Lock()

    def send_notification_event(self, event_type, username, status=None, message=None):
        """Send a notification event to all connected SSE clients"""
        try:
            with self.sse_lock:
                event_data = {
                    'type': event_type,
                    'username': username,
                    'timestamp': datetime.now().isoformat(),
                    'message': message or f'{username} {event_type}'
                }
                if status is not None:
                    event_data['status'] = status

                # Send to all active clients
                for client_queue in self.active_sse_clients[:]:  # Copy list to avoid modification during iteration
                    try:
                        client_queue.put_nowait(event_data)
                    except queue.Full:
                        # Remove client if queue is full (client likely disconnected)
                        self.active_sse_clients.remove(client_queue)
        except Exception as e:
            current_app.logger.error(f"Error sending SSE event: {e}")

    def create_event_stream(self):
        """Create SSE event stream for a client"""
        def event_stream():
            # Create a queue for this client
            client_queue = queue.Queue(maxsize=50)

            # Add client to active clients list
            with self.sse_lock:
                self.active_sse_clients.append(client_queue)

            try:
                # Send initial connection event
                initial_event = {'type': 'connected', 'message': 'Connected to notification stream'}
                yield f"data: {json.dumps(initial_event)}\n\n"

                while True:
                    try:
                        # Check for new events (timeout after 30 seconds for heartbeat)
                        try:
                            event_data = client_queue.get(timeout=30)
                            yield f"data: {json.dumps(event_data)}\n\n"
                        except queue.Empty:
                            # Send heartbeat to keep connection alive
                            heartbeat_event = {'type': 'heartbeat', 'timestamp': datetime.now().isoformat()}
                            yield f"data: {json.dumps(heartbeat_event)}\n\n"

                    except GeneratorExit:
                        # Client disconnected - no logging needed outside app context
                        break
                    except Exception as e:
                        try:
                            current_app.logger.error(f"SSE stream error: {e}")
                        except RuntimeError:
                            # App context not available, skip logging
                            pass
                        break
            finally:
                # Remove client from active clients list
                with self.sse_lock:
                    if client_queue in self.active_sse_clients:
                        self.active_sse_clients.remove(client_queue)

        return event_stream()

# Global instance
notification_service = NotificationService()