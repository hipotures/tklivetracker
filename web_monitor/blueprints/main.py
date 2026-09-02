from flask import Blueprint, render_template, current_app, send_from_directory
import os

# Create blueprint
main_bp = Blueprint('main', __name__)

def get_web_monitor_feature_flags():
    """Return feature flags used by the web UI."""
    config = current_app.config.get('CONFIG', {})
    web_monitor_config = config.get('web_monitor', {})

    return {
        'legacySchedulerStatsEnabled': bool(
            web_monitor_config.get('legacy_scheduler_stats_enabled', False)
        )
    }

@main_bp.route('/')
def dashboard():
    """Main dashboard page"""
    return render_template(
        'index.html',
        read_only=current_app.config.get('READ_ONLY', False),
        feature_flags=get_web_monitor_feature_flags()
    )

@main_bp.route('/manifest.json')
def manifest():
    """Web App Manifest file"""
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)) + '/../static', 'manifest.json', mimetype='application/json')
