"""
modules/ethereal_avatars.py — Ethereal Avatar System Integration
════════════════════════════════════════════════════════════════
Wires the ethereal skin/shader/skeleton system into PubCast's
existing avatar infrastructure.

Corrected imports to match actual codebase:
  - AvatarState from modules.avatar (Pydantic BaseModel)
  - AvatarPreset from modules.avatar_system_raw (Enum)
  - PubCastSkeleton from modules.avatar_skeleton_system

This module provides:
  - EtherealAvatarManager: lifecycle management for ethereal skins
  - FastAPI router with REST + WebSocket endpoints
  - Integration hooks for main.py lifespan

Rear View Foresight LLC — Feic Mo Chroí — 2026-03-24T01:00Z
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

logger = logging.getLogger("pubcast.ethereal_avatars")

# ─── Imports from existing PubCast modules ─────────────────────────────────────

from modules.avatar import AvatarState, list_presets, get_preset
from modules.avatar_system_raw import AvatarPreset
from modules.avatar_skeleton_system import PubCastSkeleton, create_standard_skeleton, SkeletonAnimator
from modules.persistence import read_json, write_json

# ─── Neon color palette ────────────────────────────────────────────────────────

NEON_COLORS: Dict[str, str] = {
    "blue":   "#4DC8FF",
    "pink":   "#FF4DC8",
    "purple": "#C84DFF",
    "green":  "#4DFF4D",
    "yellow": "#FFFF4D",
    "white":  "#FFFFFF",
    "black":  "#111111",
}

DEFAULT_NEON_COLOR = "blue"

# ─── Ethereal skin state ──────────────────────────────────────────────────────

@dataclass
class EtherealSkinState:
    """Runtime state for one user's ethereal avatar skin."""
    user_id: str
    skeleton: PubCastSkeleton
    neon_color: str = "blue"
    energy_flow_enabled: bool = True
    gesture_intensity: float = 1.8
    glow_intensity: float = 2.5
    opacity: float = 0.75
    gender: str = "neutral"
    current_gesture: str = "idle"
    mood: str = "calm"
    created_at: float = field(default_factory=time.time)

    @property
    def hex_color(self) -> str:
        return NEON_COLORS.get(self.neon_color, NEON_COLORS["blue"])

    def to_broadcast_dict(self) -> Dict[str, Any]:
        """Data shape sent to all clients via WebSocket."""
        return {
            "user_id":              self.user_id,
            "neon_color":           self.neon_color,
            "hex_color":            self.hex_color,
            "energy_flow_enabled":  self.energy_flow_enabled,
            "gesture_intensity":    self.gesture_intensity,
            "glow_intensity":       self.glow_intensity,
            "opacity":              self.opacity,
            "gender":               self.gender,
            "current_gesture":      self.current_gesture,
            "mood":                 self.mood,
        }

    def to_save_dict(self) -> Dict[str, Any]:
        """Data shape persisted to disk."""
        return {
            "user_id":              self.user_id,
            "neon_color":           self.neon_color,
            "energy_flow_enabled":  self.energy_flow_enabled,
            "gesture_intensity":    self.gesture_intensity,
            "glow_intensity":       self.glow_intensity,
            "opacity":              self.opacity,
            "gender":               self.gender,
            "created_at":           self.created_at,
        }


# ─── Manager ──────────────────────────────────────────────────────────────────

class EtherealAvatarManager:
    """
    Manages ethereal avatar skins for all connected users.

    Each user gets:
      - A PubCastSkeleton instance (shared 57-joint hierarchy)
      - An EtherealSkinState (neon color, effects, gesture state)
      - Persistence to data/ethereal/<user_id>.json

    The skeleton is the same for everyone — it's the standard PubCast rig.
    The skin is what makes each avatar unique: color, glow, energy flow.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir / "ethereal"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._skins: Dict[str, EtherealSkinState] = {}
        self._reference_skeleton = create_standard_skeleton()
        logger.info(
            "EtherealAvatarManager ready — reference skeleton has %d joints",
            len(self._reference_skeleton.joints),
        )

    # ── Skin lifecycle ────────────────────────────────────────────────────────

    def create_skin(
        self,
        user_id: str,
        gender: str = "neutral",
        neon_color: str = DEFAULT_NEON_COLOR,
    ) -> EtherealSkinState:
        """Create or reset an ethereal skin for a user."""
        skeleton = create_standard_skeleton()
        skin = EtherealSkinState(
            user_id=user_id,
            skeleton=skeleton,
            neon_color=neon_color if neon_color in NEON_COLORS else DEFAULT_NEON_COLOR,
            gender=gender,
        )
        self._skins[user_id] = skin
        self._save_skin(skin)
        logger.info("Created ethereal skin for %s — color=%s, gender=%s", user_id, neon_color, gender)
        return skin

    def get_skin(self, user_id: str) -> Optional[EtherealSkinState]:
        """Get the skin for a user, loading from disk if needed."""
        if user_id in self._skins:
            return self._skins[user_id]
        return self._load_skin(user_id)

    def get_or_create_skin(self, user_id: str, **kwargs) -> EtherealSkinState:
        """Get existing skin or create a new one."""
        skin = self.get_skin(user_id)
        if skin is not None:
            return skin
        return self.create_skin(user_id, **kwargs)

    def remove_skin(self, user_id: str) -> None:
        """Remove a user's skin from memory (keeps disk copy)."""
        self._skins.pop(user_id, None)

    # ── Skin modifications ────────────────────────────────────────────────────

    def set_neon_color(self, user_id: str, color: str) -> bool:
        """Change a user's neon color. Returns True on success."""
        skin = self.get_skin(user_id)
        if skin is None:
            return False
        color_lower = color.lower()
        if color_lower not in NEON_COLORS:
            return False
        skin.neon_color = color_lower
        self._save_skin(skin)
        return True

    def toggle_energy_flow(self, user_id: str) -> Optional[bool]:
        """Toggle energy flow on/off. Returns new state or None if user not found."""
        skin = self.get_skin(user_id)
        if skin is None:
            return None
        skin.energy_flow_enabled = not skin.energy_flow_enabled
        self._save_skin(skin)
        return skin.energy_flow_enabled

    def set_gesture(self, user_id: str, gesture: str, intensity: float = 1.5) -> bool:
        """Set the current gesture for enhanced faceless communication."""
        skin = self.get_skin(user_id)
        if skin is None:
            return False
        skin.current_gesture = gesture
        skin.gesture_intensity = max(0.5, min(3.0, intensity))
        return True

    def set_mood(self, user_id: str, mood: str) -> bool:
        """Set the mood for aura/energy effects."""
        skin = self.get_skin(user_id)
        if skin is None:
            return False
        skin.mood = mood
        return True

    def update_skeleton_from_performance(
        self, user_id: str, frame_data: Dict[str, Any]
    ) -> bool:
        """Apply a performance capture frame to a user's skeleton."""
        skin = self.get_skin(user_id)
        if skin is None:
            return False
        SkeletonAnimator.apply_performance_data(skin.skeleton, frame_data)
        return True

    # ── Export ─────────────────────────────────────────────────────────────────

    def export_skeleton_json(self, user_id: str, filepath: Path) -> bool:
        """Export a user's skeleton to JSON for Blender/Maya/Unreal."""
        skin = self.get_skin(user_id)
        if skin is None:
            return False
        filepath.parent.mkdir(parents=True, exist_ok=True)
        skin.skeleton.export_to_json(filepath)
        logger.info("Exported skeleton for %s to %s", user_id, filepath)
        return True

    def get_retargeting_map(self) -> Dict[str, str]:
        """Get the standard retargeting map for external rigs."""
        return self._reference_skeleton.get_retargeting_map()

    # ── Query ─────────────────────────────────────────────────────────────────

    def list_active_skins(self) -> List[Dict[str, Any]]:
        """List all active ethereal skins."""
        return [skin.to_broadcast_dict() for skin in self._skins.values()]

    def get_available_colors(self) -> Dict[str, str]:
        """Return the neon color palette."""
        return dict(NEON_COLORS)

    def get_skeleton_info(self) -> Dict[str, Any]:
        """Return info about the standard skeleton."""
        skel = self._reference_skeleton
        return {
            "joint_count": len(skel.joints),
            "joint_names": list(skel.joints.keys()),
            "retargeting_map": skel.get_retargeting_map(),
            "compatible_with": ["Blender", "Maya", "Unreal", "Unity", "Cinema4D"],
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_skin(self, skin: EtherealSkinState) -> None:
        path = self._skin_path(skin.user_id)
        write_json(path, skin.to_save_dict())

    def _load_skin(self, user_id: str) -> Optional[EtherealSkinState]:
        path = self._skin_path(user_id)
        data = read_json(path)
        if not data:
            return None
        skeleton = create_standard_skeleton()
        skin = EtherealSkinState(
            user_id=data.get("user_id", user_id),
            skeleton=skeleton,
            neon_color=data.get("neon_color", DEFAULT_NEON_COLOR),
            energy_flow_enabled=data.get("energy_flow_enabled", True),
            gesture_intensity=data.get("gesture_intensity", 1.8),
            glow_intensity=data.get("glow_intensity", 2.5),
            opacity=data.get("opacity", 0.75),
            gender=data.get("gender", "neutral"),
            created_at=data.get("created_at", time.time()),
        )
        self._skins[user_id] = skin
        return skin

    def _skin_path(self, user_id: str) -> Path:
        safe_uid = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)[:64]
        return self._data_dir / f"{safe_uid}.json"


# ═══════════════════════════════════════════════════════════════════════════════
# FastAPI Router
# ═══════════════════════════════════════════════════════════════════════════════

def create_ethereal_router(manager: EtherealAvatarManager) -> APIRouter:
    """Create the FastAPI router for ethereal avatar endpoints."""
    router = APIRouter(prefix="/api/avatars/ethereal", tags=["Ethereal Avatars"])

    @router.get("/colors")
    async def get_colors():
        return manager.get_available_colors()

    @router.get("/skeleton")
    async def get_skeleton_info():
        return manager.get_skeleton_info()

    @router.get("/active")
    async def list_active():
        return {"skins": manager.list_active_skins()}

    @router.post("/create")
    async def create_skin(request: Request):
        body = await request.json()
        user_id = body.get("user_id")
        if not user_id:
            raise HTTPException(400, "user_id required")
        skin = manager.create_skin(
            user_id=user_id,
            gender=body.get("gender", "neutral"),
            neon_color=body.get("neon_color", DEFAULT_NEON_COLOR),
        )
        return {"ok": True, "skin": skin.to_broadcast_dict()}

    @router.post("/{user_id}/color")
    async def change_color(user_id: str, request: Request):
        body = await request.json()
        color = body.get("color", "blue")
        if not manager.set_neon_color(user_id, color):
            raise HTTPException(404, f"Skin not found for {user_id} or invalid color '{color}'")
        skin = manager.get_skin(user_id)
        return {"ok": True, "skin": skin.to_broadcast_dict()}

    @router.post("/{user_id}/energy-flow")
    async def toggle_energy(user_id: str):
        result = manager.toggle_energy_flow(user_id)
        if result is None:
            raise HTTPException(404, f"Skin not found for {user_id}")
        return {"ok": True, "energy_flow_enabled": result}

    @router.post("/{user_id}/gesture")
    async def set_gesture(user_id: str, request: Request):
        body = await request.json()
        gesture = body.get("gesture", "idle")
        intensity = body.get("intensity", 1.5)
        if not manager.set_gesture(user_id, gesture, intensity):
            raise HTTPException(404, f"Skin not found for {user_id}")
        return {"ok": True, "gesture": gesture, "intensity": intensity}

    @router.post("/{user_id}/mood")
    async def set_mood(user_id: str, request: Request):
        body = await request.json()
        mood = body.get("mood", "calm")
        if not manager.set_mood(user_id, mood):
            raise HTTPException(404, f"Skin not found for {user_id}")
        return {"ok": True, "mood": mood}

    @router.post("/{user_id}/export")
    async def export_skeleton(user_id: str, request: Request):
        body = await request.json()
        format_type = body.get("format", "json")
        export_dir = manager._data_dir.parent / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        filepath = export_dir / f"{user_id}_skeleton.json"

        if not manager.export_skeleton_json(user_id, filepath):
            raise HTTPException(404, f"Skin not found for {user_id}")
        return {"ok": True, "path": str(filepath)}

    @router.get("/{user_id}")
    async def get_skin(user_id: str):
        skin = manager.get_skin(user_id)
        if skin is None:
            raise HTTPException(404, f"No ethereal skin for {user_id}")
        return skin.to_broadcast_dict()

    return router


# ═══════════════════════════════════════════════════════════════════════════════
# Hub integration — handle ethereal messages in the WebSocket stream
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_ethereal_ws_message(
    manager: EtherealAvatarManager,
    hub: Any,
    room: str,
    data: Dict[str, Any],
) -> bool:
    """
    Handle ethereal avatar WebSocket messages.
    Returns True if the message was handled, False if it should be passed through.

    Wire this into hub.handle_message() or the main WebSocket loop:

        if data.get("type", "").startswith("ethereal_") or data.get("type") in ETHEREAL_TYPES:
            handled = await handle_ethereal_ws_message(ethereal_mgr, hub, room, data)
            if handled:
                return
    """
    msg_type = data.get("type", "")
    user_id = data.get("user_id", "")

    if msg_type == "change_skin_color":
        color = data.get("color", "blue")
        success = manager.set_neon_color(user_id, color)
        if success:
            skin = manager.get_skin(user_id)
            await hub._broadcast(room, {
                "type": "skin_color_changed",
                "payload": skin.to_broadcast_dict(),
            })
        return True

    elif msg_type == "toggle_energy_flow":
        enabled = manager.toggle_energy_flow(user_id)
        if enabled is not None:
            await hub._broadcast(room, {
                "type": "energy_flow_toggled",
                "payload": {"user_id": user_id, "enabled": enabled},
            })
        return True

    elif msg_type == "enhanced_gesture":
        gesture = data.get("gesture", "idle")
        intensity = data.get("intensity", 1.5)
        manager.set_gesture(user_id, gesture, intensity)
        await hub._broadcast(room, {
            "type": "gesture_triggered",
            "payload": {"user_id": user_id, "gesture": gesture, "intensity": intensity},
        })
        return True

    elif msg_type == "avatar_mood_change":
        mood = data.get("mood", "calm")
        manager.set_mood(user_id, mood)
        skin = manager.get_skin(user_id)
        if skin:
            await hub._broadcast(room, {
                "type": "avatar_mood_changed",
                "payload": skin.to_broadcast_dict(),
            })
        return True

    elif msg_type == "skeleton_frame":
        frame_data = data.get("frame", {})
        manager.update_skeleton_from_performance(user_id, frame_data)
        # Don't broadcast skeleton frames back — the renderer handles this locally
        return True

    return False


ETHEREAL_TYPES = {
    "change_skin_color",
    "toggle_energy_flow",
    "enhanced_gesture",
    "avatar_mood_change",
    "skeleton_frame",
}
