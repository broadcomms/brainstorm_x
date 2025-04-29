

# app/sockets.py
"""
Central Socket.IO hub for BrainStormX workshops.
All real‑time events (presence, chat, ideas, lifecycle) are registered here.
Importing this module is enough to register the handlers.
"""

from collections import defaultdict
from typing import Dict, List

from flask import current_app, request
from flask_socketio import emit, join_room, leave_room

from .extensions import socketio, db
from .models import User, Workshop, WorkshopParticipant  # add Idea, ChatMessage if present

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
                "profile_pic_url": getattr(u, "profile_pic_url", "")
                or "/static/default-profile.png",
                "is_organizer": u.user_id == workshop.created_by_id,
                "email": u.email,
            }
        )
    return payload


def _broadcast_participant_list(room: str, workshop_id: int):
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
    if not info:
        return
    room, workshop_id, user_id = (
        info["room"],
        info["workshop_id"],
        info["user_id"],
    )
    _room_presence[room].discard(user_id)
    current_app.logger.debug("Client %s disconnected from %s (user %s)", request.sid, room, user_id)
    _broadcast_participant_list(room, workshop_id)


@socketio.on("join_room")
def _on_join_room(data):
    room = data.get("room")
    workshop_id = data.get("workshop_id")
    user_id = data.get("user_id")
    if not all([room, workshop_id, user_id]):
        current_app.logger.warning("join_room called with incomplete data: %s", data)
        return

    join_room(room)
    _sid_registry[request.sid] = {
        "room": room,
        "workshop_id": workshop_id,
        "user_id": user_id,
    }
    _room_presence[room].add(user_id)
    current_app.logger.debug("User %s joined %s", user_id, room)
    _broadcast_participant_list(room, workshop_id)


@socketio.on("leave_room")
def _on_leave_room(data):
    room = data.get("room")
    workshop_id = data.get("workshop_id")
    user_id = data.get("user_id")
    if not all([room, workshop_id, user_id]):
        current_app.logger.warning("leave_room called with incomplete data: %s", data)
        return

    leave_room(room)
    _room_presence[room].discard(user_id)
    # remove matching sid(s)
    for sid, info in list(_sid_registry.items()):
        if info["user_id"] == user_id and info["room"] == room:
            _sid_registry.pop(sid, None)

    current_app.logger.debug("User %s left %s", user_id, room)
    _broadcast_participant_list(room, workshop_id)


@socketio.on("request_participant_list")
def _on_request_participant_list(data):
    room = data.get("room")
    workshop_id = data.get("workshop_id")
    if not all([room, workshop_id]):
        return
    _broadcast_participant_list(room, workshop_id)


@socketio.on("send_message")
def _on_send_message(data):
    room = data.get("room")
    if not room:
        return
    # TODO: persist ChatMessage if desired
    emit("receive_message", data, to=room)


@socketio.on("submit_idea")
def _on_submit_idea(data):
    room = data.get("room")
    workshop_id = data.get("workshop_id")
    user_id = data.get("user_id")
    content = (data.get("content") or "").strip()
    if not all([room, workshop_id, user_id, content]):
        return

    # Example persistence (uncomment if Idea model exists)
    # idea = Idea(workshop_id=workshop_id, user_id=user_id, content=content)
    # db.session.add(idea)
    # db.session.commit()
    # idea_id = idea.id
    idea_id = None  # fallback if not saving

    user = User.query.get(user_id)
    user_name = user.first_name or user.email.split("@")[0] if user else "Unknown"

    emit(
        "idea_submitted",
        {"idea_id": idea_id, "user": user_name, "content": content},
        to=room,
    )


# ---------------------------------------------------------------------------
# Helper emitters for Flask routes
# ---------------------------------------------------------------------------
def emit_introduction_start(room: str, payload: dict):
    emit("introduction_start", payload, to=room)


def emit_task_ready(room: str, payload: dict):
    emit("task_ready", payload, to=room)


def emit_workshop_stopped(room: str, workshop_id: int):
    emit("workshop_stopped", {"workshop_id": workshop_id}, to=room)


def emit_workshop_paused(room: str, workshop_id: int):
    emit("workshop_paused", {"workshop_id": workshop_id}, to=room)


def emit_workshop_resumed(room: str, workshop_id: int):
    emit("workshop_resumed", {"workshop_id": workshop_id}, to=room)