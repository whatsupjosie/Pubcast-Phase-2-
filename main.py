"""
main.py — PubCast AI v5.0 — Production Entry Point
════════════════════════════════════════════════════
Boots all systems in dependency order, wires every integration hook,
and starts the FastAPI server.

Boot Sequence:
  1. Settings + directories
  2. Hub (message router)
  3. RoomManager (room state)
  4. InferenceManager (Ollama)
  5. CricketKeeper (per-character memory)
  6. BotManager (AI co-hosts)
  7. CameraManager + RecordingService
  8. GovernanceEngine (moderation, consent, waiting room)
  9. ThinkingContext (Jeremy conductor)
  10. EtherealAvatarManager (57-joint skeleton, neon skins)
  11. PubCast Vault (file protection)

Run: python main.py
Or:  uvicorn main:app --host 0.0.0.0 --port 8000

Rear View Foresight LLC — Feic Mo Chroí — 2026-03-24
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# ─── Core modules (always present) ────────────────────────────────────────────

from modules.appconfig import settings
from modules.hub import Hub
from modules.bots import BotManager
from modules.rooms import RoomManager
from modules.inference import InferenceManager
from modules.models import BotConfig, BotProvider, ProductionState
from modules.cameras import CameraManager, create_default_cameras
from modules.recording import RecordingService, create_recording_service
from modules.production_routes import create_production_router
from modules.governance import GovernanceEngine
from modules.governance_routes import create_governance_router

# ─── Optional modules (graceful degradation) ──────────────────────────────────

_HAS_CRICKET = False
_HAS_THINKING_CONTEXT = False
_HAS_ETHEREAL = False
_HAS_VAULT = False

try:
    from modules.jeremy_cricket import CricketKeeper
    _HAS_CRICKET = True
except ImportError:
    CricketKeeper = None

try:
    from thinking_context import mount as tc_mount, CharacterProfile
    _HAS_THINKING_CONTEXT = True
except ImportError:
    tc_mount = None
    CharacterProfile = None

try:
    from modules.ethereal_avatars import (
        EtherealAvatarManager, create_ethereal_router,
        handle_ethereal_ws_message, ETHEREAL_TYPES,
    )
    _HAS_ETHEREAL = True
except ImportError:
    EtherealAvatarManager = None
    ETHEREAL_TYPES = set()

try:
    from modules.pubcast_vault import PubCastVault, create_vault_router
    _HAS_VAULT = True
except ImportError:
    PubCastVault = None

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("pubcast")

# ─── Paths ─────────────────────────────────────────────────────────────────────

DATA_DIR = Path(settings.data_dir)
STATIC_DIR = Path(settings.static_dir)
ASSETS_DIR = Path(settings.assets_dir)

for d in [DATA_DIR, DATA_DIR / "logs", DATA_DIR / "users", DATA_DIR / "bots",
          DATA_DIR / "global", DATA_DIR / "jeremy", DATA_DIR / "ethereal",
          DATA_DIR / "vault", DATA_DIR / "governance", DATA_DIR / "recordings",
          DATA_DIR / "exports", DATA_DIR / "imports", STATIC_DIR, ASSETS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ─── Global refs ───────────────────────────────────────────────────────────────

hub:            Optional[Hub] = None
bot_manager:    Optional[BotManager] = None
room_manager:   Optional[RoomManager] = None
inference:      Optional[InferenceManager] = None
cameras:        Optional[CameraManager] = None
recording:      Optional[RecordingService] = None
governance:     Optional[GovernanceEngine] = None
cricket_keeper: Any = None
ethereal_mgr:   Any = None
vault:          Any = None


# ─── Adapter ───────────────────────────────────────────────────────────────────

class PubCastContextAdapter:
    """Two-method contract for ThinkingContext: get_recent_history + nudge."""
    def __init__(self, h: Hub, bm: BotManager):
        self._hub = h
        self._bm = bm

    async def get_recent_history(self, room: str, limit: int = 12) -> list:
        return await self._hub.get_recent_history(room, limit)

    async def nudge(self, room_id: str, hint: str) -> bool:
        return await self._bm.nudge(room_id, hint)


# ═══════════════════════════════════════════════════════════════════════════════
# Lifespan
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(application: FastAPI):
    global hub, bot_manager, room_manager, inference, cameras, recording
    global governance, cricket_keeper, ethereal_mgr, vault

    logger.info("═══ PubCast AI v5.0 starting ═══")
    t0 = time.time()

    # 1. Hub
    hub = Hub(DATA_DIR)
    logger.info("[1/10] Hub ready")

    # 2. RoomManager
    room_manager = RoomManager()
    logger.info("[2/10] RoomManager ready — %d rooms", len(room_manager.list_rooms()))

    # 3. Inference
    inference = InferenceManager()
    await inference.startup()
    inf_status = inference.status()
    logger.info(
        "[3/10] Inference — Studio %s, Architect %s",
        "ready" if inf_status.get("studio", {}).get("available") else "offline",
        "ready" if inf_status.get("architect", {}).get("available") else "optional/unavailable",
    )

    # 4. CricketKeeper
    if _HAS_CRICKET:
        cricket_keeper = CricketKeeper(data_dir=DATA_DIR / "jeremy")
        await cricket_keeper.init()
        logger.info("[4/10] CricketKeeper ready")
    else:
        logger.info("[4/10] CricketKeeper — not available")

    # 5. BotManager
    bot_manager = BotManager(data_dir=DATA_DIR, hub=hub)
    if cricket_keeper:
        bot_manager.set_cricket_keeper(cricket_keeper)
    hub.on_chat_callback = bot_manager.on_chat_message
    logger.info("[5/10] BotManager ready — %d bots", len(bot_manager.list_configs()))

    # 6. Cameras + Recording
    cameras = create_default_cameras()
    recording = create_recording_service(DATA_DIR, cameras)
    application.include_router(create_production_router(cameras, recording, hub))
    logger.info("[6/10] Cameras (%d) + Recording (%d profiles) ready",
                len(cameras.list_sources()), len(recording.list_profiles()))

    # 7. Governance
    governance = GovernanceEngine(DATA_DIR)
    application.include_router(create_governance_router(governance, hub))
    logger.info("[7/10] Governance ready — bans, freeze, consent, waiting room")

    # 8. ThinkingContext
    if _HAS_THINKING_CONTEXT:
        adapter = PubCastContextAdapter(hub, bot_manager)
        characters = _build_character_profiles()
        tc_mount(application, adapter, characters=characters,
                 snapshot_path=str(DATA_DIR / "memory_snapshot.json"),
                 startup_rooms=["studio", "green_room"])
        logger.info("[8/10] ThinkingContext mounted — %d characters", len(characters))
    else:
        logger.info("[8/10] ThinkingContext — not available")

    # 9. Ethereal Avatars
    if _HAS_ETHEREAL:
        ethereal_mgr = EtherealAvatarManager(DATA_DIR)
        application.include_router(create_ethereal_router(ethereal_mgr))
        logger.info("[9/10] Ethereal Avatars ready — 57 joints, %d colors",
                    len(ethereal_mgr.get_available_colors()))
    else:
        logger.info("[9/10] Ethereal Avatars — not available")

    # 10. Vault
    if _HAS_VAULT:
        vault = PubCastVault(DATA_DIR / "vault")
        application.include_router(create_vault_router(vault))
        logger.info("[10/10] Vault ready — OS-level protection active")
    else:
        logger.info("[10/10] Vault — not available")

    elapsed = time.time() - t0
    logger.info("═══ PubCast AI v5.0 ready in %.1fs ═══", elapsed)
    logger.info("    http://%s:%d", settings.host, settings.port)
    logger.info("    Airlock:      http://%s:%d/", settings.host, settings.port)
    logger.info("    Stage:        http://%s:%d/static/stage.html", settings.host, settings.port)
    logger.info("    Control Room: http://%s:%d/static/control_room.html", settings.host, settings.port)
    logger.info("    Map:          http://%s:%d/static/map.html", settings.host, settings.port)

    yield

    # Shutdown
    logger.info("═══ PubCast AI shutting down ═══")
    if vault:
        vault.shutdown()
    if cricket_keeper and hasattr(cricket_keeper, "close_all"):
        await cricket_keeper.close_all()
    logger.info("═══ PubCast AI stopped ═══")


# ═══════════════════════════════════════════════════════════════════════════════
# Application
# ═══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="PubCast AI",
    version="5.0.0",
    description="Collaborative AI-Infused Virtual Production",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("PUBCAST_ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request body size limit (1MB)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse

class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    MAX_BODY = 1_048_576  # 1MB
    async def dispatch(self, request: StarletteRequest, call_next):
        content_length = request.headers.get('content-length')
        if content_length and int(content_length) > self.MAX_BODY:
            return StarletteResponse('Request body too large', status_code=413)
        return await call_next(request)

app.add_middleware(BodySizeLimitMiddleware)

# Static files
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


# ═══════════════════════════════════════════════════════════════════════════════
# Root → Airlock
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    """Landing page — the lobby."""
    lobby = STATIC_DIR / "index.html"
    if lobby.exists():
        return FileResponse(lobby)
    airlock = STATIC_DIR / "waiting_room.html"
    if airlock.exists():
        return FileResponse(airlock)
    return {"message": "PubCast AI v5.0", "lobby": "/static/index.html"}


# ═══════════════════════════════════════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "5.0.0",
        "systems": {
            "hub": hub is not None,
            "rooms": len(room_manager.list_rooms()) if room_manager else 0,
            "bots": len(bot_manager.list_configs()) if bot_manager else 0,
            "inference": inference.status() if inference else None,
            "cameras": len(cameras.list_sources()) if cameras else 0,
            "recording_profiles": len(recording.list_profiles()) if recording else 0,
            "governance": governance is not None,
            "cricket": cricket_keeper is not None,
            "thinking_context": _HAS_THINKING_CONTEXT,
            "ethereal": _HAS_ETHEREAL and ethereal_mgr is not None,
            "vault": _HAS_VAULT and vault is not None,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# REST Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/state/production")
async def get_production_state():
    return hub.get_production_state() if hub else {}

@app.post("/api/state/production")
async def update_production_state(request: Request):
    if not hub:
        raise HTTPException(503, "Hub not initialized")
    body = await request.json()
    updated = hub.update_production_state(body)
    await hub.broadcast_system_event({"type": "production_state", "payload": updated})
    return updated

@app.get("/api/bots")
async def list_bots():
    return [cfg.dict() for cfg in bot_manager.list_configs()] if bot_manager else []

@app.post("/api/bots")
async def register_bot(request: Request):
    if not bot_manager:
        raise HTTPException(503, "BotManager not initialized")
    body = await request.json()
    config = BotConfig(**body)
    bot_manager.upsert_config(config)
    return {"ok": True, "bot_id": config.bot_id}

@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: str):
    if bot_manager:
        bot_manager.delete_config(bot_id)
    return {"ok": True}

@app.get("/api/rooms")
async def list_rooms():
    if room_manager:
        return room_manager.to_dict()
    return {"rooms": []}

@app.post("/api/inference/generate")
async def generate_text(request: Request):
    if not inference:
        raise HTTPException(503, "Inference not initialized")
    body = await request.json()
    return await inference.generate_text(
        prompt=body.get("prompt", ""),
        temperature=body.get("temperature", 0.7),
        max_tokens=body.get("max_tokens", 256),
        requested_route=body.get("route", body.get("role", "auto")),
        task_type=body.get("task_type", ""),
        user_facing=body.get("user_facing"),
        allow_fallback=body.get("allow_fallback"),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# WebSocket
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/{room}")
async def websocket_room(ws: WebSocket, room: str):
    if not hub:
        await ws.close(code=1013, reason="Hub not ready")
        return

    # Governance check — extract user_id from query params
    user_id = ws.query_params.get("user_id", "")
    if governance and user_id:
        banned, reason = governance.is_banned(user_id)
        if banned:
            await ws.close(code=4403, reason=f"Banned: {reason}")
            return
        if governance.is_muted(user_id):
            pass  # Allow connection but messages will be filtered

    await hub.connect(ws, room)
    tc = getattr(app.state, "thinking_context", None)

    if tc:
        try:
            await tc.watch_room(room)
        except Exception:
            pass

    try:
        while True:
            raw = await ws.receive_text()

            # Ethereal avatar messages
            if _HAS_ETHEREAL and ethereal_mgr:
                try:
                    parsed = json.loads(raw)
                    if parsed.get("type", "") in ETHEREAL_TYPES:
                        await handle_ethereal_ws_message(ethereal_mgr, hub, room, parsed)
                        continue
                except (json.JSONDecodeError, Exception):
                    pass

            await hub.handle_message(ws, room, raw)

            # Feed to ThinkingContext
            if tc:
                try:
                    data = json.loads(raw)
                    if data.get("type") == "chat":
                        asyncio.create_task(tc.on_message(
                            room,
                            data.get("user_id", data.get("user", "anon")),
                            data.get("text", ""),
                        ))
                except Exception:
                    pass

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("WebSocket error in %s: %s", room, exc)
    finally:
        await hub.disconnect(ws, room)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _build_character_profiles() -> list:
    if not _HAS_THINKING_CONTEXT or not CharacterProfile or not bot_manager:
        return []
    return [
        CharacterProfile(
            character_id=f"bot-{cfg.bot_id}",
            name=cfg.name,
            role="host" if cfg.auto_reply else "guest",
        )
        for cfg in bot_manager.list_configs()
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# Entry
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.host, port=settings.port,
                reload=settings.debug, log_level="debug" if settings.debug else "info")
