"""modules/inference.py — split-role inference on top of Studio/Ollama."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from .split_llm import SplitLLMManager

logger = logging.getLogger("pubcast.inference")


class InferenceManager:
    def __init__(self):
        self._worker_alive = False
        self._started_at: Optional[float] = None
        self.split_manager = SplitLLMManager()
        self._last_status: Dict[str, Any] = self.split_manager.status()

    def status(self) -> Dict[str, Any]:
        studio = self._last_status.get("studio", {})
        architect = self._last_status.get("architect", {})
        return {
            "worker_alive": self._worker_alive,
            "provider": "split_router",
            "active_role": self._last_status.get("active_role"),
            "active_provider": self._last_status.get("active_provider"),
            "default_role": self._last_status.get("default_role"),
            "studio": studio,
            "architect": architect,
            "architect_available": architect.get("available", False),
            "last_route": self._last_status.get("last_route"),
            "last_error": self._last_status.get("last_error"),
            "started_at": self._started_at,
        }

    async def startup(self):
        self._last_status = await self.split_manager.startup()
        studio = self._last_status.get("studio", {})
        architect = self._last_status.get("architect", {})
        self._worker_alive = bool(studio.get("available"))
        self._started_at = time.time()
        if studio.get("available"):
            logger.info("Studio inference ready via Ollama model %s", studio.get("model"))
        else:
            logger.warning("Studio inference unavailable: %s", studio.get("error"))
        if architect.get("available"):
            logger.info("Architect runner ready via %s", architect.get("runner_command"))
        else:
            logger.info("Architect optional provider unavailable: %s", architect.get("error"))

    async def generate_text(
        self,
        prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 256,
        requested_route: str = "auto",
        task_type: str = "",
        user_facing: Optional[bool] = None,
        allow_fallback: Optional[bool] = None,
    ) -> Dict[str, Any]:
        result = await self.split_manager.generate(
            prompt=prompt,
            requested_route=requested_route,
            task_type=task_type,
            user_facing=user_facing,
            temperature=temperature,
            max_tokens=max_tokens,
            allow_fallback=allow_fallback,
        )
        self._last_status = self.split_manager.status()
        self._worker_alive = bool(self._last_status.get("studio", {}).get("available"))
        return result

    async def synthesize_speech(self, text: str, voice: str = "default") -> Dict[str, Any]:
        return {
            "status": "tts_not_implemented",
            "text": text,
            "voice": voice,
            "note": "Connect a TTS service via OLLAMA_HOST or external API",
        }


__all__ = ["InferenceManager"]
