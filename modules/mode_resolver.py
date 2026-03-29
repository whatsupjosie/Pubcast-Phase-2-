"""
modules/mode_resolver.py — Production Mode Resolver
════════════════════════════════════════════════════
Watches the production state (on_air, recording, mode) and resolves
it into behavioral parameters that Jeremy and the bots use to adjust
their conversational style.

MODES:
  REHEARSAL  — chatty, helpful, bots experiment with material
  PRE_SHOW   — focused, countdown energy, "places everyone"
  LIVE       — careful, minimal interjections, stay on script
  POST_SHOW  — loose, celebratory, review and debrief
  IDLE       — casual conversation, low-key

Rear View Foresight LLC — Feic Mo Chroí — 2026-03-24
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger("pubcast.mode_resolver")


class ShowMode(str, Enum):
    IDLE = "idle"
    REHEARSAL = "rehearsal"
    PRE_SHOW = "pre_show"
    LIVE = "live"
    POST_SHOW = "post_show"


@dataclass
class ModeParameters:
    """Behavioral parameters derived from the current show mode."""
    mode: ShowMode
    nudge_cooldown: float        # seconds between Jeremy nudges
    nudge_threshold: float       # sigmoid threshold for necessity
    bot_reply_delay: float       # min seconds before a bot responds
    bot_max_length: int          # max tokens for bot responses
    bot_personality_boost: float # multiplier on personality expressiveness
    system_prompt_suffix: str    # appended to every bot system prompt
    allow_bot_to_bot: bool       # whether bots can respond to each other


# Predefined parameter sets
MODE_PARAMS: Dict[ShowMode, ModeParameters] = {
    ShowMode.IDLE: ModeParameters(
        mode=ShowMode.IDLE,
        nudge_cooldown=30.0,
        nudge_threshold=0.3,
        bot_reply_delay=2.0,
        bot_max_length=256,
        bot_personality_boost=1.0,
        system_prompt_suffix="This is casual downtime. Be relaxed and conversational.",
        allow_bot_to_bot=True,
    ),
    ShowMode.REHEARSAL: ModeParameters(
        mode=ShowMode.REHEARSAL,
        nudge_cooldown=15.0,
        nudge_threshold=0.2,
        bot_reply_delay=1.5,
        bot_max_length=384,
        bot_personality_boost=1.2,
        system_prompt_suffix="This is a rehearsal. Try out material, be creative, experiment. "
                             "Mistakes are welcome — that's what rehearsal is for.",
        allow_bot_to_bot=True,
    ),
    ShowMode.PRE_SHOW: ModeParameters(
        mode=ShowMode.PRE_SHOW,
        nudge_cooldown=20.0,
        nudge_threshold=0.4,
        bot_reply_delay=2.0,
        bot_max_length=200,
        bot_personality_boost=0.9,
        system_prompt_suffix="The show is about to start. Keep it focused. "
                             "Help everyone get ready. Encourage, don't distract.",
        allow_bot_to_bot=False,
    ),
    ShowMode.LIVE: ModeParameters(
        mode=ShowMode.LIVE,
        nudge_cooldown=45.0,
        nudge_threshold=0.6,
        bot_reply_delay=3.0,
        bot_max_length=150,
        bot_personality_boost=0.7,
        system_prompt_suffix="YOU ARE LIVE ON AIR. Be concise. Stay on topic. "
                             "Only speak when you have something valuable to add. "
                             "Do not interrupt. Keep responses short and punchy.",
        allow_bot_to_bot=False,
    ),
    ShowMode.POST_SHOW: ModeParameters(
        mode=ShowMode.POST_SHOW,
        nudge_cooldown=20.0,
        nudge_threshold=0.25,
        bot_reply_delay=1.5,
        bot_max_length=300,
        bot_personality_boost=1.3,
        system_prompt_suffix="The show just wrapped! Celebrate, debrief, share highlights. "
                             "Be warm, enthusiastic, and supportive. Great work everyone!",
        allow_bot_to_bot=True,
    ),
}


class ModeResolver:
    """
    Resolves the current production state into behavioral parameters.

    Usage:
        resolver = ModeResolver()
        params = resolver.resolve(production_state)
        # Use params.nudge_cooldown, params.bot_max_length, etc.

    Auto-detection:
        resolver.update_from_hub(hub)  # reads production state
        params = resolver.current_params()
    """

    def __init__(self) -> None:
        self._current_mode = ShowMode.IDLE
        self._mode_entered_at = time.time()
        self._override: Optional[ShowMode] = None

    @property
    def current_mode(self) -> ShowMode:
        return self._override or self._current_mode

    def current_params(self) -> ModeParameters:
        return MODE_PARAMS[self.current_mode]

    def resolve(self, production_state: Dict[str, Any]) -> ModeParameters:
        """
        Resolve production state dict into mode parameters.

        Logic:
          - on_air=True → LIVE
          - on_air=False + mode=LIVE → POST_SHOW (just ended)
          - mode=REPLAY → POST_SHOW
          - recording active but not on_air → REHEARSAL
          - otherwise → IDLE
        """
        on_air = production_state.get("on_air", False)
        mode_str = production_state.get("mode", "LIVE").upper()
        camera = production_state.get("camera", "")

        if on_air:
            new_mode = ShowMode.LIVE
        elif mode_str == "REPLAY":
            new_mode = ShowMode.POST_SHOW
        elif camera == "black" and self._current_mode == ShowMode.LIVE:
            new_mode = ShowMode.POST_SHOW
        elif self._current_mode == ShowMode.LIVE and not on_air:
            new_mode = ShowMode.POST_SHOW
        else:
            new_mode = ShowMode.IDLE

        if new_mode != self._current_mode:
            logger.info("Mode changed: %s → %s", self._current_mode.value, new_mode.value)
            self._current_mode = new_mode
            self._mode_entered_at = time.time()

        return MODE_PARAMS[self._override or self._current_mode]

    def set_override(self, mode: ShowMode) -> None:
        """Manually override the mode (e.g., host forces REHEARSAL)."""
        self._override = mode
        logger.info("Mode override set: %s", mode.value)

    def clear_override(self) -> None:
        self._override = None
        logger.info("Mode override cleared")

    def time_in_current_mode(self) -> float:
        return time.time() - self._mode_entered_at

    def status(self) -> Dict[str, Any]:
        params = self.current_params()
        return {
            "mode": self.current_mode.value,
            "override": self._override.value if self._override else None,
            "time_in_mode": round(self.time_in_current_mode(), 1),
            "nudge_cooldown": params.nudge_cooldown,
            "nudge_threshold": params.nudge_threshold,
            "bot_reply_delay": params.bot_reply_delay,
            "bot_max_length": params.bot_max_length,
            "allow_bot_to_bot": params.allow_bot_to_bot,
        }
