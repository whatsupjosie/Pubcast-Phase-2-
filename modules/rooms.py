"""
modules/rooms.py
----------------
Room management for PubCast AI.
Provides Participant, Room, and RoomManager — the structural backbone
for organizing who is in which space and routing messages appropriately.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from .schemas import ParticipantRole

logger = logging.getLogger("pubcast.rooms")


@dataclass
class Participant:
    """
    A single participant in a PubCast room — human or bot.
    """
    participant_id: str
    display_name:   str
    role:           ParticipantRole = ParticipantRole.GUEST
    user_id:        Optional[str]   = None
    bot_id:         Optional[str]   = None
    joined_at:      float           = field(default_factory=time.time)
    metadata:       Dict[str, Any]  = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        display_name: str,
        role: ParticipantRole = ParticipantRole.GUEST,
        user_id: Optional[str] = None,
        bot_id:  Optional[str] = None,
        **metadata: Any,
    ) -> "Participant":
        return cls(
            participant_id=str(uuid.uuid4()),
            display_name=display_name,
            role=role,
            user_id=user_id,
            bot_id=bot_id,
            metadata=metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "display_name":   self.display_name,
            "role":           self.role.value,
            "user_id":        self.user_id,
            "bot_id":         self.bot_id,
            "joined_at":      self.joined_at,
            "metadata":       self.metadata,
        }


class Room:
    """
    A single named room that holds participants and a message log.
    Thread-safe for concurrent WebSocket access.
    """

    def __init__(self, room_id: str, display_name: str = "", max_participants: int = 100):
        self.room_id        = room_id
        self.display_name   = display_name or room_id
        self.max_participants = max_participants
        self.created_at     = time.time()
        self._participants: Dict[str, Participant] = {}
        self._message_log:  List[Dict[str, Any]]   = []

    # ── Participants ─────────────────────────────────────────────────────────

    def add_participant(self, participant: Participant) -> bool:
        if len(self._participants) >= self.max_participants:
            logger.warning("Room %s is at capacity (%d)", self.room_id, self.max_participants)
            return False
        self._participants[participant.participant_id] = participant
        logger.info("Participant %s joined room %s", participant.display_name, self.room_id)
        return True

    def remove_participant(self, participant_id: str) -> Optional[Participant]:
        p = self._participants.pop(participant_id, None)
        if p:
            logger.info("Participant %s left room %s", p.display_name, self.room_id)
        return p

    def get_participant(self, participant_id: str) -> Optional[Participant]:
        return self._participants.get(participant_id)

    def list_participants(self) -> List[Participant]:
        return list(self._participants.values())

    @property
    def participant_count(self) -> int:
        return len(self._participants)

    # ── Message log ──────────────────────────────────────────────────────────

    def log_message(
        self,
        participant_id: str,
        text: str,
        msg_type: str = "chat",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "id":             str(uuid.uuid4()),
            "room_id":        self.room_id,
            "participant_id": participant_id,
            "type":           msg_type,
            "text":           text,
            "timestamp":      time.time(),
            **(metadata or {}),
        }
        self._message_log.append(entry)
        # Keep last 500 messages in memory
        if len(self._message_log) > 500:
            self._message_log = self._message_log[-500:]
        return entry

    def get_recent_messages(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._message_log[-limit:]

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "room_id":           self.room_id,
            "display_name":      self.display_name,
            "participant_count": self.participant_count,
            "participants":      [p.to_dict() for p in self.list_participants()],
            "created_at":        self.created_at,
        }


class RoomManager:
    """
    Manages the full set of PubCast rooms.
    Rooms are created on demand and persist for the lifetime of the process.
    """

    # Default rooms that always exist
    DEFAULT_ROOMS = [
        ("hub",         "Hub"),
        ("vortex",      "Vortex Bar"),
        ("basement",    "Basement"),
        ("green_room",  "Green Room"),
        ("writers",     "Writers / Conference Room"),
        ("control",     "Control Room"),
        ("studio",      "Studio Floor"),
        ("on_air",      "On Air Stage"),
    ]

    def __init__(self):
        self._rooms: Dict[str, Room] = {}
        self._callbacks: List[Callable[[str, str, Dict[str, Any]], None]] = []
        self._init_default_rooms()

    def _init_default_rooms(self) -> None:
        for room_id, display_name in self.DEFAULT_ROOMS:
            self._rooms[room_id] = Room(room_id, display_name)
        logger.info("RoomManager initialised with %d default rooms", len(self._rooms))

    # ── Room CRUD ────────────────────────────────────────────────────────────

    def get_or_create(self, room_id: str, display_name: str = "") -> Room:
        if room_id not in self._rooms:
            self._rooms[room_id] = Room(room_id, display_name or room_id)
            logger.info("Created new room: %s", room_id)
        return self._rooms[room_id]

    def get(self, room_id: str) -> Optional[Room]:
        return self._rooms.get(room_id)

    def list_rooms(self) -> List[Room]:
        return list(self._rooms.values())

    def delete_room(self, room_id: str) -> bool:
        if room_id in self._rooms:
            del self._rooms[room_id]
            logger.info("Deleted room: %s", room_id)
            return True
        return False

    # ── Participant helpers ───────────────────────────────────────────────────

    def join(
        self,
        room_id: str,
        participant: Participant,
    ) -> bool:
        room = self.get_or_create(room_id)
        return room.add_participant(participant)

    def leave(self, room_id: str, participant_id: str) -> Optional[Participant]:
        room = self.get(room_id)
        if not room:
            return None
        return room.remove_participant(participant_id)

    def find_participant(self, participant_id: str) -> Optional[tuple[str, Participant]]:
        """Search all rooms for a participant. Returns (room_id, participant) or None."""
        for room_id, room in self._rooms.items():
            p = room.get_participant(participant_id)
            if p:
                return room_id, p
        return None

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rooms": [r.to_dict() for r in self.list_rooms()],
            "total_participants": sum(r.participant_count for r in self._rooms.values()),
        }


__all__ = ["Participant", "Room", "RoomManager"]
