"""modules/choreography_controller.py — Server-side avatar animation tick."""
from __future__ import annotations
import asyncio, logging, time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pubcast.choreo")

class ChoreoController:
    def __init__(self, hub, tick_hz: float = 30.0):
        self._hub = hub; self._tick_hz = tick_hz
        self._running = False; self._task = None
        self._room: Optional[str] = None
        self._avatars: List[str] = []; self._steps: List[Dict] = []

    def status(self) -> Dict[str, Any]:
        return {"running": self._running, "room": self._room,
                "tick_hz": self._tick_hz, "avatars": self._avatars}

    async def start(self, room: str, avatars=None, steps=None, tick_hz=None) -> Dict:
        if self._running: await self.stop()
        self._room = room; self._avatars = avatars or []
        self._steps = steps or []
        if tick_hz: self._tick_hz = float(tick_hz)
        self._running = True
        self._task = asyncio.create_task(self._loop())
        return {"ok": True, "room": room, "tick_hz": self._tick_hz}

    async def stop(self):
        self._running = False
        if self._task: self._task.cancel()
        self._task = None

    async def _loop(self):
        interval = 1.0 / self._tick_hz
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._hub.broadcast_system_event(
                    {"type": "choreo_tick", "payload": {"room": self._room, "t": time.time()}})
            except Exception as exc:
                logger.warning("ChoreoController: broadcast tick failed (room=%s): %s", self._room, exc)

__all__ = ["ChoreoController"]
