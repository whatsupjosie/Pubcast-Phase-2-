"""
modules/avatar.py
-----------------
Avatar persistence API for PubCast AI.

Provides the four functions that main.py needs:
  - AvatarState  (data model)
  - list_presets()
  - load_avatar(data_dir, user_id)
  - save_avatar(data_dir, user_id, state)

The MotionSystem / full avatar runtime lives in avatar_system_raw.py
and is imported here for completeness; this thin wrapper adds the
persistence layer that main.py depends on.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("pubcast.avatar")


def _normalize_hex_color(value: str, fallback: str = "#00FFFF") -> str:
    if not value:
        return fallback
    value = str(value).strip()
    if not value.startswith("#"):
        value = "#" + value
    if len(value) == 4:
        value = "#" + "".join(ch * 2 for ch in value[1:])
    if len(value) != 7:
        return fallback
    try:
        int(value[1:], 16)
    except ValueError:
        return fallback
    return value.upper()


def _normalize_avatar_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(payload or {})
    if "preset_id" in normalized and "preset" not in normalized:
        normalized["preset"] = normalized["preset_id"]
    if "displayName" in normalized and "display_name" not in normalized:
        normalized["display_name"] = normalized["displayName"]
    if "glowColor" in normalized and "glow_color" not in normalized:
        normalized["glow_color"] = normalized["glowColor"]
    if "title" in normalized and "badge" not in normalized:
        normalized["badge"] = normalized["title"]
    if "preset" in normalized and normalized["preset"]:
        normalized["preset"] = str(normalized["preset"]).upper()
    normalized["glow_color"] = _normalize_hex_color(normalized.get("glow_color", "#00FFFF"))
    if "energy_level" in normalized:
        try:
            normalized["energy_level"] = max(0.0, min(2.0, float(normalized["energy_level"])))
        except Exception:
            normalized["energy_level"] = 1.0
    if "visible" in normalized:
        normalized["visible"] = bool(normalized["visible"])
    return normalized


# ── Data model ────────────────────────────────────────────────────────────────

class AvatarState(BaseModel):
    """
    Persisted avatar configuration for a user.
    Stored at data/avatars/<user_id>.json
    """
    user_id:        str
    preset:         str        = "MANNY"
    glow_color:     str        = "#00FFFF"
    display_name:   str        = "User"
    energy_level:   float      = Field(default=1.0, ge=0.0, le=2.0)
    visible:        bool       = True
    hologram_mode:  str        = "energy"           # energy | ghost | solid
    accessories:    List[str]  = Field(default_factory=list)
    metadata:       Dict[str, Any] = Field(default_factory=dict)
    updated_at:     float      = Field(default_factory=time.time)

    class Config:
        # pydantic v1 compat
        extra = "allow"


# ── Presets registry ──────────────────────────────────────────────────────────

_PRESET_REGISTRY: Dict[str, Dict[str, Any]] = {
    "MANNY": {
        "id":           "MANNY",
        "name":         "Manny",
        "description":  "Classic humanoid energy being",
        "glow_color":   "#00FFFF",
        "body_type":    "humanoid",
        "height":       1.0,
        "emoji":        "🧍",
        "silhouette":   "classic",
    },
    "SHELA": {
        "id":           "SHELA",
        "name":         "Shela",
        "description":  "Lithe feminine silhouette",
        "glow_color":   "#FF69B4",
        "body_type":    "humanoid",
        "height":       0.95,
        "emoji":        "✨",
        "silhouette":   "graceful",
    },
    "MAN_LARGE": {
        "id":           "MAN_LARGE",
        "name":         "Man Large",
        "description":  "Broad shouldered energy form",
        "glow_color":   "#4488FF",
        "body_type":    "humanoid",
        "height":       1.1,
        "emoji":        "🛡️",
        "silhouette":   "broad",
    },
    "MAN_SMALL": {
        "id":           "MAN_SMALL",
        "name":         "Man Small",
        "description":  "Compact humanoid energy form",
        "glow_color":   "#44FFAA",
        "body_type":    "humanoid",
        "height":       0.88,
        "emoji":        "⚡",
        "silhouette":   "compact",
    },
    "CHILD": {
        "id":           "CHILD",
        "name":         "Child",
        "description":  "Small youthful energy form",
        "glow_color":   "#FFDD00",
        "body_type":    "child",
        "height":       0.72,
        "emoji":        "🌟",
        "silhouette":   "spry",
    },
    "BABY": {
        "id":           "BABY",
        "name":         "Baby",
        "description":  "Tiny floating energy orb",
        "glow_color":   "#FFFFFF",
        "body_type":    "baby",
        "height":       0.5,
        "emoji":        "🫧",
        "silhouette":   "orb",
    },
    "DOG": {
        "id":           "DOG",
        "name":         "Dog",
        "description":  "Four-legged energy companion",
        "glow_color":   "#FF8C00",
        "body_type":    "quadruped",
        "height":       0.65,
        "emoji":        "🐾",
        "silhouette":   "companion",
    },
    "GHOST": {
        "id":           "GHOST",
        "name":         "Ghost",
        "description":  "Wispy semi-transparent spectral form",
        "glow_color":   "#AAEEFF",
        "body_type":    "ghost",
        "height":       1.0,
        "emoji":        "👻",
        "silhouette":   "spectral",
    },
}


class AvatarPresetInfo(BaseModel):
    id:          str
    preset_id:   Optional[str] = None
    name:        str
    description: str = ""
    glow_color:  str = "#00FFFF"
    body_type:   str = "humanoid"
    height:      float = 1.0
    emoji:       str = "👤"
    silhouette:  str = "classic"

    class Config:
        extra = "allow"


def list_presets() -> List[AvatarPresetInfo]:
    """Return all available avatar presets as Pydantic models."""
    presets: List[AvatarPresetInfo] = []
    for p in _PRESET_REGISTRY.values():
        payload = dict(p)
        payload.setdefault("preset_id", payload.get("id"))
        payload["glow_color"] = _normalize_hex_color(payload.get("glow_color", "#00FFFF"))
        presets.append(AvatarPresetInfo(**payload))
    return presets


def get_preset(preset_id: str) -> Optional[Dict[str, Any]]:
    """Return a single preset by ID, or None."""
    return _PRESET_REGISTRY.get(preset_id.upper())


# ── Persistence helpers ───────────────────────────────────────────────────────

def _avatar_path(data_dir: Path, user_id: str) -> Path:
    d = Path(data_dir) / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    # Sanitise user_id so it's safe as a filename
    safe_uid = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)[:64]
    return d / f"{safe_uid}.json"


def load_avatar(data_dir: Path, user_id: str) -> AvatarState:
    """Load a user's avatar state from disk. Returns default if not found."""
    path = _avatar_path(data_dir, user_id)
    if path.exists():
        try:
            data = _normalize_avatar_payload(json.loads(path.read_text(encoding="utf-8")))
            data["user_id"] = user_id  # always authoritative
            return AvatarState(**data)
        except Exception as exc:
            logger.warning("Could not load avatar for %s: %s", user_id, exc)
    return AvatarState(user_id=user_id)


def save_avatar(data_dir: Path, user_id: str, state: AvatarState) -> AvatarState:
    """
    Persist an AvatarState to disk.
    Accepts either an AvatarState instance or a dict.
    Returns the saved AvatarState.
    """
    if isinstance(state, dict):
        # Merge with existing to preserve unlisted fields
        existing = load_avatar(data_dir, user_id)
        merged = existing.dict()
        merged.update(_normalize_avatar_payload(state))
        merged["user_id"] = user_id
        merged["updated_at"] = time.time()
        state = AvatarState(**merged)
    else:
        state.user_id = user_id
        state.glow_color = _normalize_hex_color(state.glow_color)
        state.preset = str(state.preset or "MANNY").upper()
        state.updated_at = time.time()

    path = _avatar_path(data_dir, user_id)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(state.json(), encoding="utf-8")
        tmp.replace(path)
        logger.debug("Saved avatar for %s", user_id)
    except Exception as exc:
        logger.error("Could not save avatar for %s: %s", user_id, exc)
    return state


__all__ = [
    "AvatarState",
    "list_presets",
    "get_preset",
    "load_avatar",
    "save_avatar",
]
