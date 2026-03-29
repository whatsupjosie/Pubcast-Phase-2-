"""
modules/lighting.py — PubCast AI Lighting System
==================================================
Standard presets designed by Josie Curtsey Cobbley.
Implements the Purkinje Effect for perceived-realism moonlight.

Presets:
  standard_night  — "Standard Full Moon" (cinematic realism)
  day             — Bright studio daylight
  golden_hour     — Warm late afternoon
  van_gogh        — PubWorld artistic mode (swirling, stylized)
  cyberpunk       — Neon city night
  broadcast       — Neutral broadcast white
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import time

@dataclass
class LightSource:
    name: str
    hex_color: str
    temp_kelvin: int
    intensity: float      # 0.0–1.0
    cast_shadows: bool
    shadow_softness: float  # 0.0 = hard, 1.0 = soft
    direction_deg: float = 60.0  # angle above horizon

@dataclass
class SkySettings:
    gradient_top: str
    gradient_bottom: str
    cloud_mode: str        # STATIC | VOLUMETRIC | VAN_GOGH | NONE
    cloud_density: float   # 0.0–1.0
    star_intensity: float  # 0.0–1.0
    bloom: bool = False
    fog_density: float = 0.0

@dataclass
class LightingPreset:
    preset_id: str
    name: str
    description: str
    lights: List[LightSource]
    sky: SkySettings
    global_illumination: str = "Lumen"
    volumetric_fog: bool = False
    fog_density: float = 0.0
    transition_seconds: float = 1.5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "preset_id": self.preset_id,
            "name": self.name,
            "description": self.description,
            "lights": [vars(l) for l in self.lights],
            "sky": vars(self.sky),
            "global_illumination": self.global_illumination,
            "volumetric_fog": self.volumetric_fog,
            "fog_density": self.fog_density,
            "transition_seconds": self.transition_seconds,
        }


# ── Preset Definitions ────────────────────────────────────────────────────────

def StandardNightMode() -> LightingPreset:
    """
    Standard Full Moon — Cinematic realism.
    Implements Purkinje Effect: steel-blue key, rich-blue fill, warm anchor.
    Designed by Josie Curtsey Cobbley / Gemini collaboration session.
    #C2D2E8 @ 7500K = perceived moonlight. #FFB46B @ 2800K = safety anchor.
    """
    return LightingPreset(
        preset_id="standard_night",
        name="Standard Full Moon",
        description="Cinematic night. Cool moon, warm anchor. Still. Safe.",
        lights=[
            LightSource(
                name="Lunar Key",
                hex_color="#C2D2E8",
                temp_kelvin=7500,
                intensity=0.85,
                cast_shadows=True,
                shadow_softness=0.2,
                direction_deg=68.0,
            ),
            LightSource(
                name="Atmospheric Fill",
                hex_color="#101626",
                temp_kelvin=9000,
                intensity=0.12,
                cast_shadows=False,
                shadow_softness=1.0,
                direction_deg=0.0,
            ),
            LightSource(
                name="Warm Practical",
                hex_color="#FFB46B",
                temp_kelvin=2800,
                intensity=0.45,
                cast_shadows=True,
                shadow_softness=0.8,
                direction_deg=15.0,
            ),
        ],
        sky=SkySettings(
            gradient_top="#050814",
            gradient_bottom="#101626",
            cloud_mode="STATIC",
            cloud_density=0.3,
            star_intensity=0.6,
            bloom=True,
            fog_density=0.02,
        ),
        volumetric_fog=True,
        fog_density=0.02,
        transition_seconds=2.0,
    )


def DayMode() -> LightingPreset:
    return LightingPreset(
        preset_id="day",
        name="Bright Day",
        description="Clean broadcast daylight.",
        lights=[
            LightSource("Sun", "#FFF5E0", 5600, 1.0, True, 0.15, 72.0),
            LightSource("Sky Fill", "#C8D8F0", 7000, 0.35, False, 1.0, 90.0),
        ],
        sky=SkySettings("#87CEEB", "#C8E6F0", "NONE", 0.0, 0.0),
        transition_seconds=1.5,
    )


def GoldenHourMode() -> LightingPreset:
    return LightingPreset(
        preset_id="golden_hour",
        name="Golden Hour",
        description="Warm late-afternoon magic.",
        lights=[
            LightSource("Setting Sun", "#FF8C42", 2900, 0.9, True, 0.6, 12.0),
            LightSource("Sky Fill",    "#FFD1A0", 3500, 0.2, False, 1.0, 30.0),
        ],
        sky=SkySettings("#FF6B35", "#FFD1A0", "STATIC", 0.15, 0.0),
        transition_seconds=2.0,
    )


def VanGoghMode() -> LightingPreset:
    """PubWorld artistic mode. Swirling, expressive, not realistic."""
    return LightingPreset(
        preset_id="van_gogh",
        name="Van Gogh Sky",
        description="PubWorld artistic mode. Swirling expressionist sky.",
        lights=[
            LightSource("Moon", "#D4E8FF", 8000, 0.7, True, 0.3, 55.0),
            LightSource("Azure Fill", "#1A3A6A", 9500, 0.3, False, 1.0, 0.0),
        ],
        sky=SkySettings("#0A1628", "#1A3A6A", "VAN_GOGH", 0.8, 0.9, bloom=True),
        volumetric_fog=True,
        fog_density=0.04,
        transition_seconds=3.0,
    )


def CyberpunkMode() -> LightingPreset:
    return LightingPreset(
        preset_id="cyberpunk",
        name="Cyberpunk Night",
        description="Neon city. Magenta and cyan. Rain-soaked.",
        lights=[
            LightSource("Neon Magenta", "#FF006E", 3200, 0.6, True, 0.5, 20.0),
            LightSource("Neon Cyan",    "#00D4FF", 8500, 0.5, True, 0.6, 35.0),
            LightSource("Street Amber", "#FF8800", 2400, 0.3, False, 0.8, 5.0),
        ],
        sky=SkySettings("#0D0014", "#1A0028", "STATIC", 0.5, 0.2, bloom=True),
        volumetric_fog=True,
        fog_density=0.06,
        transition_seconds=1.0,
    )


def BroadcastMode() -> LightingPreset:
    return LightingPreset(
        preset_id="broadcast",
        name="Broadcast White",
        description="Neutral, flat, professional studio lighting.",
        lights=[
            LightSource("Key",  "#FFFFFF", 5500, 0.85, True,  0.4, 45.0),
            LightSource("Fill", "#F5F5F5", 5600, 0.4,  False, 0.7, 30.0),
            LightSource("Back", "#FFFAEA", 5200, 0.3,  True,  0.3, 70.0),
        ],
        sky=SkySettings("#E8EFF5", "#D0DDE8", "NONE", 0.0, 0.0),
        transition_seconds=0.8,
    )


# ── Manager ───────────────────────────────────────────────────────────────────

_PRESET_REGISTRY: Dict[str, LightingPreset] = {}

def _build_registry() -> None:
    for fn in [StandardNightMode, DayMode, GoldenHourMode,
               VanGoghMode, CyberpunkMode, BroadcastMode]:
        p = fn()
        _PRESET_REGISTRY[p.preset_id] = p

_build_registry()

_active_preset: str = "broadcast"

class LightingManager:
    def list_presets(self) -> List[str]:
        return list(_PRESET_REGISTRY.keys())

    def get_preset(self, preset_id: str) -> Optional[LightingPreset]:
        return _PRESET_REGISTRY.get(preset_id)

    def get_active(self) -> Optional[LightingPreset]:
        return _PRESET_REGISTRY.get(_active_preset)

    async def apply_preset(self, preset_id: str) -> Dict[str, Any]:
        global _active_preset
        preset = _PRESET_REGISTRY.get(preset_id)
        if not preset:
            return {"ok": False, "error": f"Unknown preset: {preset_id}"}
        _active_preset = preset_id
        return {
            "ok": True,
            "preset": preset.to_dict(),
            "applied_at": time.time(),
        }
