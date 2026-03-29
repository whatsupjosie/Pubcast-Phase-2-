
from __future__ import annotations
import asyncio
import json
import time
from pathlib import Path
from typing import Dict, Any, Set, List
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from .persistence import read_json, write_json, append_jsonl
from .models import ProductionState

class Hub:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.rooms: Dict[str, Set[WebSocket]] = {}
        self.user_display_name: Dict[str, str] = {}  # uid -> display name (optional; for presence)
        # production state on disk
        self.prod_file = self.data_dir / "global" / "production.json"
        if not self.prod_file.parent.exists():
            self.prod_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.prod_file.exists():
            write_json(self.prod_file, ProductionState().dict())
        self.on_chat_callback = None

    def get_production_state(self) -> Dict[str, Any]:
        return read_json(self.prod_file)

    def update_production_state(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get_production_state()
        # merge shallowly
        current.update(patch or {})
        # validate through model
        model = ProductionState(**current)
        write_json(self.prod_file, model.dict())
        return model.dict()

    async def connect(self, ws: WebSocket, room: str):
        await ws.accept()
        self.rooms.setdefault(room, set()).add(ws)
        # send recent history
        history_path = self._log_path(room)
        last_msgs: List[str] = []
        if history_path.exists():
            # send last 50 lines
            with open(history_path, "r", encoding="utf-8") as f:
                last_msgs = f.readlines()[-50:]
        if last_msgs:
            await ws.send_text(json.dumps({"type": "history", "payload": [json.loads(l) for l in last_msgs]}))
        # announce presence
        await self._broadcast(room, {"type": "presence", "payload": {"room": room, "count": len(self.rooms.get(room, []))}})

    async def disconnect(self, ws: WebSocket, room: str):
        try:
            self.rooms.get(room, set()).discard(ws)
        finally:
            await self._broadcast(room, {"type": "presence", "payload": {"room": room, "count": len(self.rooms.get(room, []))}})
            if ws.application_state == WebSocketState.CONNECTED:
                await ws.close()

    async def broadcast_system_event(self, event: Dict[str, Any]):
        for room, conns in self.rooms.items():
            await self._broadcast(room, event)

    async def broadcast_presence_update(self, user_id: str, display_name: str):
        # Could be extended to map sockets to user ids to show names; minimal for now
        self.user_display_name[user_id] = display_name
        await self.broadcast_system_event({"type": "user_update", "payload": {"user_id": user_id, "display_name": display_name}})

    async def handle_message(self, ws: WebSocket, room: str, raw: str):
        # Expect raw to be JSON string
        try:
            data = json.loads(raw)
        except Exception:
            return
        typ = data.get("type")
        if typ == "chat":
            payload = {
                "t": time.time(),
                "room": room,
                "user": data.get("user", "anon"),
                "user_id": data.get("user_id") or data.get("user") or "anon",
                "text": data.get("text", ""),
            }
            append_jsonl(self._log_path(room), payload)
            await self._broadcast(room, {"type": "chat", "payload": payload})
            if callable(self.on_chat_callback):
                await self.on_chat_callback(payload)
        elif typ == "signal":
            # extension point for invites / cues
            await self._broadcast(room, {"type": "signal", "payload": data.get("payload", {})})
        elif typ == "production_state":
            # allow control room to push updates via ws (UI convenience)
            updated = self.update_production_state(data.get("payload", {}))
            await self.broadcast_system_event({"type": "production_state", "payload": updated})
        else:
            # echo unknown
            await ws.send_text(json.dumps({"type": "error", "payload": {"msg": "unknown type"}}))

    async def _broadcast(self, room: str, msg: Dict[str, Any]):
        dead: List[WebSocket] = []
        for ws in list(self.rooms.get(room, set())):
            try:
                if ws.application_state == WebSocketState.CONNECTED:
                    await ws.send_text(json.dumps(msg))
                else:
                    dead.append(ws)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.rooms.get(room, set()).discard(ws)

    def _log_path(self, room: str) -> Path:
        logs_dir = self.data_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        return logs_dir / f"{room}.jsonl"

    # ── Methods required by BotManager ───────────────────────────────────────

    async def is_user_in_room(self, room: str, user_id: str) -> bool:
        """Return True if any WebSocket in the room was registered by user_id."""
        # We track uid→ws mapping lazily; approximate by checking the user map.
        # Full presence tracking is a Phase-3 enhancement; for now return True
        # when the user has a display name registered (means they connected at
        # some point this session) — conservative but bot-safe default.
        return user_id in self.user_display_name

    async def get_recent_history(self, room: str, limit: int = 12) -> list:
        """Return the last `limit` chat messages from a room's log.

        File I/O is dispatched to a thread via run_in_executor so the async
        event loop is never blocked - safe to call from Jeremy's watcher loop.
        """
        history_path = self._log_path(room)
        if not history_path.exists():
            return []
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._read_history_sync, history_path, limit
        )

    def _read_history_sync(self, path: Path, limit: int) -> list:
        """Synchronous helper - runs in a thread pool, not on the event loop."""
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        recent = lines[-limit:] if len(lines) > limit else lines
        result = []
        for line in recent:
            try:
                result.append(json.loads(line))
            except Exception:
                continue
        return result

    async def post_chat_message(self, room: str, user_id: str, text: str) -> None:
        """Post a chat message on behalf of a bot/agent into a room."""
        display_name = self.user_display_name.get(user_id, user_id)
        payload = {
            "t": time.time(),
            "room": room,
            "user": display_name,
            "user_id": user_id,
            "text": text,
        }
        append_jsonl(self._log_path(room), payload)
        await self._broadcast(room, {"type": "chat", "payload": payload})
        if callable(self.on_chat_callback):
            await self.on_chat_callback(payload)

    def invalidate_user_cache(self, user_id: str) -> None:
        """No-op cache invalidation hook (extended in future presence layer)."""
        self.user_display_name.pop(user_id, None)
