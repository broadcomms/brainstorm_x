# app/moderator.py
from datetime import datetime, timedelta
from flask import current_app
from collections import defaultdict

from app.extensions import socketio
from app.models import Workshop, BrainstormTask # Import necessary models
from app.config import TASK_SEQUENCE # Import task sequence

# --- In-memory storage for tracking ---
# { workshop_id: { user_id: last_submission_timestamp } }
workshop_last_submission = defaultdict(dict)
# { workshop_id: { user_id: last_nudge_timestamp } }
workshop_last_nudge = defaultdict(dict)

# --- Configuration ---
NUDGE_THRESHOLD_SECONDS = 30  # Nudge if inactive for 60 seconds
NUDGE_COOLDOWN_SECONDS = 120 # Don't nudge the same user more than once every 120 seconds

def initialize_participant_tracking(workshop_id, user_id):
    """Record when a participant joins."""
    now = datetime.utcnow()
    if workshop_id not in workshop_last_submission:
        workshop_last_submission[workshop_id] = {}
    if workshop_id not in workshop_last_nudge:
        workshop_last_nudge[workshop_id] = {}

    # Set initial 'last submission' time to now; they'll be nudged if inactive
    workshop_last_submission[workshop_id][user_id] = now
    current_app.logger.debug(f"[Moderator] Initialized tracking for user {user_id} in workshop {workshop_id}")

def cleanup_participant_tracking(workshop_id, user_id):
    """Remove participant data when they leave."""
    if workshop_id in workshop_last_submission and user_id in workshop_last_submission[workshop_id]:
        del workshop_last_submission[workshop_id][user_id]
    if workshop_id in workshop_last_nudge and user_id in workshop_last_nudge[workshop_id]:
        del workshop_last_nudge[workshop_id][user_id]
    current_app.logger.debug(f"[Moderator] Cleaned up tracking for user {user_id} in workshop {workshop_id}")

def clear_workshop_tracking(workshop_id):
    """Clear all tracking data for a finished workshop."""
    if workshop_id in workshop_last_submission:
        del workshop_last_submission[workshop_id]
    if workshop_id in workshop_last_nudge:
        del workshop_last_nudge[workshop_id]
    current_app.logger.info(f"[Moderator] Cleared all tracking for workshop {workshop_id}")


def check_and_nudge(workshop_id, submitter_user_id, current_participants_in_room):
    """Checks inactivity and sends nudges via Socket.IO."""
    now = datetime.utcnow()
    workshop = Workshop.query.get(workshop_id)

    # --- Validation: Only nudge during active brainstorming ---
    if not workshop or workshop.status != 'inprogress':
        return
    current_task = workshop.current_task
    if not current_task or workshop.current_task_index is None:
        return
    current_task_type = TASK_SEQUENCE[workshop.current_task_index] if 0 <= workshop.current_task_index < len(TASK_SEQUENCE) else "unknown"
    if current_task_type not in ["warm-up", "brainstorming"]: # Only nudge during these phases
        current_app.logger.debug(f"[Moderator] Skipping nudge, current task type is {current_task_type}")
        return
    # ---------------------------------------------------------

    # Update submitter's last submission time
    if workshop_id in workshop_last_submission:
        workshop_last_submission[workshop_id][submitter_user_id] = now
        current_app.logger.debug(f"[Moderator] Updated last submission for user {submitter_user_id} in workshop {workshop_id}")

    # Check other participants
    for user_id in current_participants_in_room:
        if user_id == submitter_user_id:
            continue # Don't nudge the person who just submitted

        last_submission = workshop_last_submission.get(workshop_id, {}).get(user_id)
        last_nudge = workshop_last_nudge.get(workshop_id, {}).get(user_id)

        if last_submission:
            time_since_submission = (now - last_submission).total_seconds()
            time_since_nudge = (now - last_nudge).total_seconds() if last_nudge else float('inf')

            if time_since_submission > NUDGE_THRESHOLD_SECONDS and time_since_nudge > NUDGE_COOLDOWN_SECONDS:
                # --- Emit nudge to specific user ---
                target_sid = None
                # Find the SID for the target user (requires _sid_registry access or modification)
                # For now, we assume a way to get the SID or emit to a user-specific room if implemented.
                # Simplified: Emitting to the main room, client JS needs to check if it's for them.
                # A better approach involves user-specific rooms or SID mapping.
                socketio.emit('moderator_nudge',
                              {'message': "Keep the ideas flowing!", 'target_user_id': user_id},
                              room=f'workshop_room_{workshop_id}') # Emit to the main room
                workshop_last_nudge[workshop_id][user_id] = now # Record nudge time
                current_app.logger.info(f"[Moderator] Nudged user {user_id} in workshop {workshop_id}")