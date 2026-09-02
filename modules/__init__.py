# This file makes the 'modules' directory a Python package.

# Provide backward compatibility for modules.db imports
import sys

# Create a dummy db module for backward compatibility
class DBModule:
    pass

db = DBModule()

# Import all functions from the new modules
from .db_setup import create_connection, initialize_db
from .db_user import (
    set_user_active_status, add_user, set_is_live, update_next_check,
    get_user_id, get_reference_time, update_user_check_interval,
    get_user_active_status, reset_user_is_live, update_next_check_with_interval,
    get_user_check_interval, add_live_session, end_live_session
)
from .db_users import (
    reset_is_live_all, get_previous_recording_users, reset_stuck_live_users_and_prioritize_check,
    get_all_usernames, get_all_db_usernames, get_next_user_to_check, get_all_users_data,
    get_live_users, get_users_with_current_timestamp, get_eligible_users_to_check,
    reset_all_live_users_next_check
)

# Add all functions to the db module
for name, func in list(globals().items()):
    if callable(func) and not name.startswith('_'):
        setattr(db, name, func)