"""
modules/schemas.py
------------------
Shared schema / enum types consumed by rooms, orchestrator,
and WebSocket event routing.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
import time


class ParticipantRole(str, Enum):
    HOST      = "host"
    GUEST     = "guest"
    BOT       = "bot"
    MODERATOR = "moderator"
    VIEWER    = "viewer"
    HUMAN     = "human"


# ── WebSocket event payloads ──────────────────────────────────────────────────

@dataclass
class SayEvent:
    """A participant says something in a room."""
    room_id:      str
    participant_id: str
    text:         str
    timestamp:    float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "say",
            "room_id": self.room_id,
            "participant_id": self.participant_id,
            "text": self.text,
            "timestamp": self.timestamp,
        }


@dataclass
class StreamChunkEvent:
    """A streaming text chunk from a bot."""
    room_id:      str
    participant_id: str
    chunk:        str
    timestamp:    float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "stream_chunk",
            "room_id": self.room_id,
            "participant_id": self.participant_id,
            "chunk": self.chunk,
            "timestamp": self.timestamp,
        }


@dataclass
class DoneEvent:
    """Signals end of a streaming or async operation."""
    room_id:      str
    participant_id: str
    reason:       str = "complete"
    timestamp:    float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "done",
            "room_id": self.room_id,
            "participant_id": self.participant_id,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


@dataclass
class ErrorEvent:
    """Signals an error to a room."""
    room_id:      str
    participant_id: str
    error:        str
    timestamp:    float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "error",
            "room_id": self.room_id,
            "participant_id": self.participant_id,
            "error": self.error,
            "timestamp": self.timestamp,
        }


@dataclass
class RoomSnapshot:
    """Full state of a room at a point in time."""
    room_id:      str
    participants: List[Dict[str, Any]]
    message_count: int = 0
    timestamp:    float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "room_snapshot",
            "room_id": self.room_id,
            "participants": self.participants,
            "message_count": self.message_count,
            "timestamp": self.timestamp,
        }


__all__ = [
    "ParticipantRole",
    "SayEvent",
    "StreamChunkEvent",
    "DoneEvent",
    "ErrorEvent",
    "RoomSnapshot",
]
