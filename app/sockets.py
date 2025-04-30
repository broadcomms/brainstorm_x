# app/sockets.py
"""
Central Socket.IO hub for BrainStormX workshops.
All real‑time events (presence, chat, ideas, lifecycle) are registered here.
Importing this module is enough to register the handlers.
"""
import json
from collections import defaultdict
from typing import Dict, List
from datetime import datetime # Ensure datetime is imported

from flask import current_app, request
from flask_socketio import emit, join_room, leave_room

# --- ADD THIS IMPORT ---
from sqlalchemy.orm import selectinload

from .extensions import socketio, db
from .models import User, Workshop, WorkshopParticipant, ChatMessage, BrainstormTask, BrainstormIdea

# ---------------------------------------------------------------------------
# In‑memory presence tracking
# ---------------------------------------------------------------------------
# sid ➜ {room, workshop_id, user_id}
_sid_registry: Dict[str, Dict] = {}
# room ➜ set(user_id)
_room_presence: Dict[str, set] = defaultdict(set)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------
def _get_participant_payload(workshop_id: int) -> List[dict]:
    """Return minimal participant info for the given workshop_id."""
    workshop = Workshop.query.get(workshop_id)
    if not workshop:
        return []

    online_ids = {
        info["user_id"]
        for info in _sid_registry.values()
        if info.get("workshop_id") == workshop_id
    }
    if not online_ids:
        return []

    users = User.query.filter(User.user_id.in_(online_ids)).all()
    payload = []
    for u in users:
        payload.append(
            {
                "user_id": u.user_id,
                "first_name": u.first_name or "",
                "last_name": u.last_name or "", # Added last_name
                "profile_pic_url": getattr(u, "profile_pic_url", None), # Pass actual URL or None
                "is_organizer": u.user_id == workshop.created_by_id,
                "email": u.email,
            }
        )
    return payload


def _broadcast_participant_list(room: str, workshop_id: int):
        """Broadcasts the list of currently connected participants to the room."""
        emit(
            "participant_list_update",
            {
                "workshop_id": workshop_id,
                "participants": _get_participant_payload(workshop_id),
            },
            to=room,
        )


# ---------------------------------------------------------------------------
# Core Socket.IO events
# ---------------------------------------------------------------------------
@socketio.on("connect")
def _on_connect():
    current_app.logger.debug("Client %s connected", request.sid)


@socketio.on("disconnect")
def _on_disconnect():
    info = _sid_registry.pop(request.sid, None)
    if info:
        room, workshop_id, user_id = info["room"], info["workshop_id"], info["user_id"]
        # Check if room still exists in presence tracking before discarding
        if room in _room_presence:
            _room_presence[room].discard(user_id)
            current_app.logger.debug(f"Client {request.sid} disconnected from {room} (user {user_id})")
            # Check if room still has participants before broadcasting
            if _room_presence[room]:
                 _broadcast_participant_list(room, workshop_id)
            else:
                 # Clean up empty room entry if no one is left
                 del _room_presence[room]
                 current_app.logger.debug(f"Cleaned up empty room: {room}")
        else:
             current_app.logger.warning(f"Room {room} not found in presence tracking during disconnect for SID {request.sid}.")


@socketio.on("join_room")
def _on_join_room(data):
    """Handles client joining a room, sends current state."""
    room = data.get("room")
    workshop_id = data.get("workshop_id")
    user_id = data.get("user_id")
    sid = request.sid # Get current session ID

    if not all([room, workshop_id, user_id]):
        current_app.logger.warning(f"join_room incomplete data from {sid}: {data}")
        return

    # --- Prevent duplicate joins for the same user/workshop in registry ---
    existing_sid = None
    for s, info in list(_sid_registry.items()): # Iterate over a copy
        if info.get("workshop_id") == workshop_id and info.get("user_id") == user_id:
            existing_sid = s
            break
    if existing_sid and existing_sid != sid:
        current_app.logger.warning(f"User {user_id} already in room {room} with SID {existing_sid}. Removing old entry.")
        _sid_registry.pop(existing_sid, None) # Remove old entry
        if room in _room_presence:
            _room_presence[room].discard(user_id) # Ensure presence count is correct

    # --- Join and Register ---
    join_room(room)
    _sid_registry[sid] = {
        "room": room,
        "workshop_id": workshop_id,
        "user_id": user_id,
    }
    # Ensure the room exists in _room_presence before adding
    if room not in _room_presence:
        _room_presence[room] = set()
    _room_presence[room].add(user_id)
    current_app.logger.info(f"User {user_id} (SID: {sid}) joined {room}")

    # --- Broadcast updated participant list ---
    _broadcast_participant_list(room, workshop_id)

    # --- Load and emit persistent data TO THE JOINING CLIENT ONLY ---
    # Use a try-except block for database access
    try:
        workshop = Workshop.query.get(workshop_id)
        if not workshop:
            current_app.logger.error(f"Workshop {workshop_id} not found for join_room event.")
            emit("error_joining", {"message": "Workshop not found."}, to=sid) # Inform client
            return

        # --- Emit Current Workshop Status ---
        emit("workshop_status_update", {
            "workshop_id": workshop_id,
            "status": workshop.status
        }, to=sid)
        current_app.logger.debug(f"Emitted status '{workshop.status}' to {sid}")

        # --- Emit Current Task and Timer State ---
        if workshop.current_task_id and workshop.current_task:
            task = workshop.current_task
            remaining_seconds = workshop.get_remaining_task_time()
            current_app.logger.debug(f"Workshop {workshop_id} has active task {task.id}. Remaining time: {remaining_seconds}s")


            # Determine if it's the intro task based on index or title convention
            is_intro = workshop.current_task_index == -1 # Or check task.title

            # Parse the prompt (assuming it's stored as JSON)
            task_details = {}
            try:
                task_details = json.loads(task.prompt) if task.prompt else {}
            except json.JSONDecodeError:
                current_app.logger.warning(f"Could not parse task prompt JSON for task {task.id}")
                # Fallback: use raw prompt as description if parsing fails
                task_details = {"task_description": task.prompt or "No description available."}

            event_name = "introduction_start" if is_intro else "task_ready"
            payload = {
                "task_id": task.id,
                "title": task.title,
                "duration": task.duration,
                # Add specific fields based on event type
                **(task_details if is_intro else {
                     "description": task_details.get("task_description", "No description."),
                     "instructions": task_details.get("instructions", "Submit ideas.")
                })
            }
            # If intro, add specific intro fields if they exist in task_details
            if is_intro:
                payload.update({k: task_details[k] for k in ["welcome", "goals", "rules", "task"] if k in task_details})


            current_app.logger.debug(f"Emitting {event_name} to {sid} for task {task.id} with duration {task.duration}")
            emit(event_name, payload, to=sid)

            # Emit timer sync information
            current_app.logger.debug(f"Emitting timer_sync to {sid} with remaining: {remaining_seconds}s, paused: {workshop.status == 'paused'}")
            emit("timer_sync", {
                "task_id": task.id, # Include task_id for context
                "remaining_seconds": remaining_seconds,
                "is_paused": workshop.status == 'paused' # Let client know if paused
            }, to=sid)
        else:
             current_app.logger.debug(f"Workshop {workshop_id} has no active task.")


        # --- Emit Whiteboard Content (Current Task's Ideas) ---
        if workshop.current_task_id:
            # Ensure participant relationship is loaded for ideas
            ideas = BrainstormIdea.query.options(
                selectinload(BrainstormIdea.participant).selectinload(WorkshopParticipant.user)
            ).filter_by(task_id=workshop.current_task_id).order_by(BrainstormIdea.timestamp).all()

            ideas_payload = []
            for idea in ideas:
                # Access user through the preloaded relationships
                user = idea.participant.user if idea.participant else None
                user_display_name = user.first_name or user.email.split('@')[0] if user else "Unknown"
                ideas_payload.append({
                    "idea_id": idea.id,
                    "user": user_display_name,
                    "content": idea.content,
                    "timestamp": idea.timestamp.isoformat() # Ensure consistent format
                })
            if ideas_payload:
                 current_app.logger.debug(f"Emitting whiteboard_sync to {sid} with {len(ideas_payload)} ideas for task {workshop.current_task_id}")
                 emit("whiteboard_sync", {"ideas": ideas_payload}, to=sid)
            else:
                 current_app.logger.debug(f"No ideas found to sync for task {workshop.current_task_id} to {sid}")
                 # Optionally emit empty sync event if needed by client logic
                 emit("whiteboard_sync", {"ideas": []}, to=sid)


        # --- Emit Chat History ---
        # Limit history to avoid overwhelming client (e.g., last 50 messages)
        chat_history = ChatMessage.query.filter_by(workshop_id=workshop_id)\
                                        .order_by(ChatMessage.timestamp.desc())\
                                        .limit(50)\
                                        .all()
        chat_history.reverse() # Put them back in chronological order

        history_payload = []
        for msg in chat_history:
            history_payload.append({
                "user_name": msg.username,
                "message": msg.message,
                "timestamp": msg.timestamp.isoformat() # Ensure consistent format
            })
        if history_payload:
            current_app.logger.debug(f"Emitting chat_history to {sid} with {len(history_payload)} messages")
            emit("chat_history", {"messages": history_payload}, to=sid)
        else:
            current_app.logger.debug(f"No chat history found to sync for workshop {workshop_id} to {sid}")
            # Optionally emit empty history if needed by client
            # emit("chat_history", {"messages": []}, to=sid)

    except Exception as e:
        current_app.logger.error(f"Error during join_room state emission for workshop {workshop_id}, SID {sid}: {e}", exc_info=True)
        # Optionally inform the client about the error
        emit("error_joining", {"message": "Error retrieving workshop state."}, to=sid)
        
        
            
@socketio.on("leave_room")
def _on_leave_room(data):
    # This logic seems okay, just ensure logging is informative
    room = data.get("room")
    workshop_id = data.get("workshop_id")
    user_id = data.get("user_id")
    sid = request.sid

    if not all([room, workshop_id, user_id]):
        current_app.logger.warning(f"leave_room incomplete data from {sid}: {data}")
        return

    leave_room(room)
    if room in _room_presence: # Check if room exists before discarding
        _room_presence[room].discard(user_id)
    # Remove the specific SID that emitted leave_room
    if sid in _sid_registry:
        _sid_registry.pop(sid)
        current_app.logger.info(f"User {user_id} (SID: {sid}) left {room}")
    else:
         current_app.logger.warning(f"SID {sid} emitted leave_room but was not in registry for room {room}.")

    # Broadcast updated list if room still active
    if room in _room_presence and _room_presence[room]:
        _broadcast_participant_list(room, workshop_id)
    elif room in _room_presence:
        del _room_presence[room] # Clean up empty room


@socketio.on("request_participant_list")
def _on_request_participant_list(data):
    # This seems fine
    room = data.get("room")
    workshop_id = data.get("workshop_id")
    if not all([room, workshop_id]): return
    _broadcast_participant_list(room, workshop_id)


@socketio.on("send_message")
def _on_send_message(data):
    # This seems mostly fine, ensure username is fetched correctly
    room = data.get("room")
    message = data.get("message", "").strip()
    user_id = data.get("user_id")
    workshop_id = data.get("workshop_id")
    if not all([room, message, user_id, workshop_id]): return

    user = User.query.get(user_id)
    if not user: return # Ignore if user not found

    # Check if workshop exists and is active (optional, prevents chat in ended workshops)
    workshop = Workshop.query.get(workshop_id)
    if not workshop or workshop.status not in ['inprogress', 'paused', 'scheduled']: # Allow chat in lobby too
        current_app.logger.warning(f"Chat message attempt in inactive workshop {workshop_id}")
        return

    username = user.first_name or user.email.split("@")[0]

    try:
        chat_message = ChatMessage(
            workshop_id=workshop_id,
            user_id=user_id,
            username=username, # Store the username
            message=message,
            timestamp=datetime.utcnow() # Ensure timestamp is set
        )
        db.session.add(chat_message)
        db.session.commit()

        emit("receive_message", {
            "user_name": chat_message.username,
            "message": chat_message.message,
            "timestamp": chat_message.timestamp.isoformat(),
            "room": room # Keep room if needed client-side, but 'to=room' handles delivery
        }, to=room)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving chat message for workshop {workshop_id}: {e}")
        # Optionally emit an error back to the sender
        emit("chat_error", {"message": "Failed to send message."}, to=request.sid)





# ---------------------------------------------------------------------------
# Helper emitters for Flask routes (Keep these)
# ---------------------------------------------------------------------------
def emit_introduction_start(room: str, payload: dict):
    """Emits the introduction task payload."""
    socketio.emit("introduction_start", payload, to=room)
    current_app.logger.info(f"Emitted introduction_start to {room}")

def emit_task_ready(room: str, payload: dict):
    """Emits the next task payload."""
    socketio.emit("task_ready", payload, to=room)
    current_app.logger.info(f"Emitted task_ready to {room} for task {payload.get('task_id')}")

def emit_workshop_stopped(room: str, workshop_id: int):
    """Notifies clients the workshop has stopped."""
    socketio.emit("workshop_stopped", {"workshop_id": workshop_id}, to=room)
    current_app.logger.info(f"Emitted workshop_stopped to {room}")

def emit_workshop_paused(room: str, workshop_id: int):
    """Notifies clients the workshop is paused."""
    socketio.emit("workshop_paused", {"workshop_id": workshop_id}, to=room)
    current_app.logger.info(f"Emitted workshop_paused to {room}")

def emit_workshop_resumed(room: str, workshop_id: int):
    """Notifies clients the workshop is resumed."""
    socketio.emit("workshop_resumed", {"workshop_id": workshop_id}, to=room)
    current_app.logger.info(f"Emitted workshop_resumed to {room}")

# --- ADDED: Generic Status Update Emitter ---
def emit_workshop_status_update(room: str, workshop_id: int, status: str):
    """Notifies clients of a general status change."""
    socketio.emit("workshop_status_update", {"workshop_id": workshop_id, "status": status}, to=room)
    current_app.logger.info(f"Emitted workshop_status_update ({status}) to {room}")