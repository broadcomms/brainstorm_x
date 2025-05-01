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
from .config import TASK_SEQUENCE # <-- ADD THIS IMPORT

from .extensions import socketio, db
from .models import User, Workshop, WorkshopParticipant, ChatMessage, BrainstormTask, BrainstormIdea, IdeaCluster, IdeaVote # Add Cluster/Vote
from sqlalchemy import func # For counting votes
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


# --- ADDED: Voting Handler ---
@socketio.on('submit_vote')
def _on_submit_vote(data):
    """Handles a user casting or retracting a vote for a cluster."""
    room = data.get("room") # e.g., workshop_room_123
    cluster_id = data.get("cluster_id")
    user_id = data.get("user_id")
    workshop_id = data.get("workshop_id")
    # action = data.get("action", "increment") # 'increment' or 'decrement'

    if not all([room, cluster_id, user_id, workshop_id]):
        current_app.logger.warning(f"submit_vote incomplete data: {data}")
        emit("vote_error", {"message": "Invalid vote data."}, to=request.sid)
        return

    # --- Validation ---
    workshop = Workshop.query.get(workshop_id)
    if not workshop or workshop.status != 'inprogress':
        emit("vote_error", {"message": "Voting is not active."}, to=request.sid)
        return

    # Check if current task is the voting task (requires identifying voting tasks)
    current_task = workshop.current_task
    if not current_task or TASK_SEQUENCE[workshop.current_task_index] != "clustering_voting": # Check sequence
         emit("vote_error", {"message": "Not the voting phase."}, to=request.sid)
         return

    # Check timer
    remaining_time = workshop.get_remaining_task_time()
    if remaining_time <= 0:
         emit("vote_error", {"message": "Time for voting has expired."}, to=request.sid)
         return

    participant = WorkshopParticipant.query.filter_by(workshop_id=workshop_id, user_id=user_id).first()
    cluster = IdeaCluster.query.get(cluster_id)

    if not participant or not cluster or cluster.task_id != current_task.id:
        emit("vote_error", {"message": "Invalid participant or cluster."}, to=request.sid)
        return

    # --- Process Vote ---
    existing_vote = IdeaVote.query.filter_by(cluster_id=cluster_id, participant_id=participant.id).first()

    try:
        new_dots_remaining = participant.dots_remaining
        vote_action_taken = None # 'voted', 'unvoted'

        if existing_vote:
            # User already voted for this cluster, so retract the vote
            db.session.delete(existing_vote)
            new_dots_remaining += 1 # Give dot back
            vote_action_taken = 'unvoted'
            current_app.logger.info(f"User {user_id} unvoted for cluster {cluster_id}")
        elif participant.dots_remaining > 0:
            # User has dots and hasn't voted for this cluster yet, cast vote
            new_vote = IdeaVote(cluster_id=cluster_id, participant_id=participant.id)
            db.session.add(new_vote)
            new_dots_remaining -= 1 # Use a dot
            vote_action_taken = 'voted'
            current_app.logger.info(f"User {user_id} voted for cluster {cluster_id}")
        else:
            # No dots left
            emit("vote_error", {"message": "You have no dots left."}, to=request.sid)
            return # Don't proceed

        # Update participant's dot count
        participant.dots_remaining = new_dots_remaining
        db.session.commit()

        # --- Calculate New Total Votes for the Cluster ---
        # Use SQLAlchemy's func.count
        total_votes_for_cluster = db.session.query(func.count(IdeaVote.id)).filter_by(cluster_id=cluster_id).scalar() or 0

        # --- Broadcast Update ---
        emit("vote_update", {
            "cluster_id": cluster_id,
            "total_votes": total_votes_for_cluster,
            "user_id": user_id, # User who triggered the update
            "dots_remaining": new_dots_remaining, # That user's new dot count
            "action_taken": vote_action_taken # 'voted' or 'unvoted'
        }, to=room) # Broadcast to everyone in the room

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error processing vote for cluster {cluster_id} by user {user_id}: {e}", exc_info=True)
        emit("vote_error", {"message": "Server error processing vote."}, to=request.sid)








# --- ADD NEW EMITTERS ---
def emit_clusters_ready(room: str, payload: dict):
    """Emits cluster data and voting instructions."""
    socketio.emit("clusters_ready", payload, to=room)
    current_app.logger.info(f"Emitted clusters_ready to {room} for task {payload.get('task_id')}")

def emit_feasibility_ready(room: str, payload: dict):
    """Emits feasibility report."""
    socketio.emit("feasibility_ready", payload, to=room)
    current_app.logger.info(f"Emitted feasibility_ready to {room} for task {payload.get('task_id')}")

def emit_summary_ready(room: str, payload: dict):
    """Emits workshop summary."""
    socketio.emit("summary_ready", payload, to=room)
    current_app.logger.info(f"Emitted summary_ready to {room} for task {payload.get('task_id')}")











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
        workshop = Workshop.query.options(
            selectinload(Workshop.current_task) # Eager load task
        ).get(workshop_id)
        if not workshop: # ... handle workshop not found ...
            return

        # Emit Current Workshop Status
        emit("workshop_status_update", {"workshop_id": workshop_id, "status": workshop.status}, to=sid)

        # --- Emit Current Task State (Handles Different Types) ---
        if workshop.current_task_id and workshop.current_task:
            task = workshop.current_task
            remaining_seconds = workshop.get_remaining_task_time()
            current_task_index = workshop.current_task_index if workshop.current_task_index is not None else -1

            # Determine current task type based on index
            current_task_type = TASK_SEQUENCE[current_task_index] if 0 <= current_task_index < len(TASK_SEQUENCE) else "unknown"
            if current_task_index == -1: current_task_type = "warm-up" # Special case for intro

            current_app.logger.debug(f"Syncing state for task {task.id} (Type: {current_task_type}, Index: {current_task_index})")

            # Parse the prompt data (should be JSON)
            task_details = {}
            try:
                task_details = json.loads(task.prompt) if task.prompt else {}
            except json.JSONDecodeError:
                current_app.logger.warning(f"Could not parse task prompt JSON for task {task.id}")
                task_details = {"error": "Could not load task details."} # Fallback

            # Determine event name and payload based on type
            event_name = "task_ready" # Default
            payload = {
                "task_id": task.id,
                "title": task.title,
                "duration": task.duration,
                "task_type": current_task_type,
                 # Include all details parsed from the prompt
                **task_details
            }

            if current_task_type == "warm-up":
                event_name = "introduction_start"
            elif current_task_type == "clustering_voting":
                event_name = "clusters_ready"
                # Add participant dot info to payload for sync
                participants_data = WorkshopParticipant.query.filter_by(workshop_id=workshop_id, status='accepted').all()
                payload['participants_dots'] = {part.user_id: part.dots_remaining for part in participants_data}
            elif current_task_type == "results_feasibility":
                event_name = "feasibility_ready"
            elif current_task_type == "summary":
                event_name = "summary_ready"
            # else: event_name remains "task_ready" for brainstorming, discussion

            current_app.logger.debug(f"Emitting {event_name} to {sid} for task {task.id}")
            emit(event_name, payload, to=sid)

            # Emit timer sync information
            emit("timer_sync", {
                "task_id": task.id,
                "remaining_seconds": remaining_seconds,
                "is_paused": workshop.status == 'paused'
            }, to=sid)

            # --- Emit Whiteboard/Cluster Content ---
            if current_task_type in ["warm-up", "brainstorming"]:
                # Emit ideas for brainstorming/warmup
                ideas = BrainstormIdea.query.options(
                    selectinload(BrainstormIdea.participant).selectinload(WorkshopParticipant.user)
                ).filter_by(task_id=task.id).order_by(BrainstormIdea.timestamp).all()
                ideas_payload = [{
                    "idea_id": idea.id,
                    "user": idea.participant.user.first_name or idea.participant.user.email.split('@')[0] if idea.participant and idea.participant.user else "Unknown",
                    "content": idea.content,
                    "timestamp": idea.timestamp.isoformat()
                } for idea in ideas]
                emit("whiteboard_sync", {"ideas": ideas_payload}, to=sid)
                current_app.logger.debug(f"Emitted whiteboard_sync with {len(ideas_payload)} ideas to {sid}")

            elif current_task_type == "clustering_voting":
                 # For voting phase, whiteboard shows clusters, not individual ideas
                 # The cluster data is already in the 'clusters_ready' payload.
                 # We might need to emit vote counts separately if not included initially.
                 clusters_with_votes = IdeaCluster.query.options(
                     selectinload(IdeaCluster.votes)
                 ).filter_by(task_id=task.id).all()
                 votes_payload = {
                     cluster.id: len(cluster.votes) for cluster in clusters_with_votes
                 }
                 emit("all_votes_sync", {"votes": votes_payload}, to=sid) # New event for initial vote counts
                 current_app.logger.debug(f"Emitted all_votes_sync with counts for {len(votes_payload)} clusters to {sid}")


        else:
             current_app.logger.debug(f"Workshop {workshop_id} has no active task upon join.")
             # Optionally emit an event to clear the task area on the client
             emit("no_active_task", {}, to=sid)


        # --- Emit Chat History ---
        # (Keep existing chat history emission logic)
        chat_history = ChatMessage.query.filter_by(workshop_id=workshop_id)\
                                        .order_by(ChatMessage.timestamp.desc())\
                                        .limit(50)\
                                        .all()
        chat_history.reverse()
        history_payload = [{
            "user_name": msg.username, "message": msg.message, "timestamp": msg.timestamp.isoformat()
        } for msg in chat_history]
        emit("chat_history", {"messages": history_payload}, to=sid)

    except Exception as e:
        current_app.logger.error(f"Error during join_room state emission for workshop {workshop_id}, SID {sid}: {e}", exc_info=True)
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