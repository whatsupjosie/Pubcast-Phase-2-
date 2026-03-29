"""
modules/governance_routes.py — Governance REST API
═══════════════════════════════════════════════════
FastAPI router for moderation, consent, waiting room, AI disclosure.

Usage in main.py:
    from modules.governance import GovernanceEngine
    from modules.governance_routes import create_governance_router
    gov = GovernanceEngine(DATA_DIR)
    app.include_router(create_governance_router(gov, hub))

Rear View Foresight LLC — Feic Mo Chroí — 2026-03-24T09:30Z
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from modules.governance import (
    GovernanceEngine, BanType, ConsentType, EntryStatus,
)

logger = logging.getLogger("pubcast.governance_routes")


def create_governance_router(gov: GovernanceEngine, hub: Any = None) -> APIRouter:
    router = APIRouter(prefix="/api/governance", tags=["Governance"])

    # ═══ CONSENT & ENTRY ═══════════════════════════════════════════════════

    @router.get("/terms")
    async def get_terms():
        return {"terms": gov.get_terms_of_service()}

    @router.get("/ai-disclosure")
    async def get_ai_disclosure():
        # Pull bot names from hub if available
        bot_names = ["Pete", "Sir Purfluous", "Jeremy Cricket"]
        return {"disclosure": gov.get_ai_disclosure(bot_names)}

    @router.post("/consent")
    async def record_consent(request: Request):
        body = await request.json()
        user_id = body.get("user_id", "")
        consent_type = body.get("consent_type", "")
        granted = body.get("granted", False)
        if not user_id or not consent_type:
            raise HTTPException(400, "user_id and consent_type required")
        try:
            ct = ConsentType(consent_type)
        except ValueError:
            raise HTTPException(400, f"Invalid consent type: {consent_type}")
        record = gov.record_consent(user_id, ct, granted)
        return {"ok": True, "consent_type": ct.value, "granted": granted}

    @router.get("/consent/{user_id}")
    async def get_consents(user_id: str):
        return gov.get_all_consents(user_id)

    @router.get("/consent/{user_id}/check")
    async def check_entry(user_id: str):
        allowed, msg = gov.can_enter(user_id)
        missing = gov.get_missing_consents(user_id)
        return {
            "allowed": allowed,
            "message": msg,
            "missing_consents": [c.value for c in missing],
        }

    # ═══ WAITING ROOM ══════════════════════════════════════════════════════

    @router.post("/waiting-room/request")
    async def request_entry(request: Request):
        body = await request.json()
        user_id = body.get("user_id", "")
        display_name = body.get("display_name", "Guest")
        target_room = body.get("target_room", "studio")
        consents = body.get("consents", {})
        if not user_id:
            raise HTTPException(400, "user_id required")
        entry = gov.request_entry(user_id, display_name, target_room, consents)
        # Notify host via WebSocket
        if hub:
            import asyncio
            asyncio.create_task(hub.broadcast_system_event({
                "type": "waiting_room_request",
                "payload": {
                    "entry_id": entry.entry_id,
                    "user_id": user_id,
                    "display_name": display_name,
                    "target_room": target_room,
                },
            }))
        return {"ok": True, "entry_id": entry.entry_id, "status": entry.status.value}

    @router.get("/waiting-room")
    async def list_waiting(room: str = None):
        entries = gov.list_waiting(room)
        return {
            "waiting": [
                {
                    "entry_id": e.entry_id,
                    "user_id": e.user_id,
                    "display_name": e.display_name,
                    "target_room": e.target_room,
                    "requested_at": e.requested_at,
                }
                for e in entries
            ]
        }

    @router.post("/waiting-room/{entry_id}/approve")
    async def approve_entry(entry_id: str, request: Request):
        body = await request.json()
        approved_by = body.get("approved_by", "host")
        entry = gov.approve_entry(entry_id, approved_by)
        if not entry:
            raise HTTPException(404, "Entry not found or already resolved")
        if hub:
            import asyncio
            asyncio.create_task(hub.broadcast_system_event({
                "type": "entry_approved",
                "payload": {"entry_id": entry_id, "user_id": entry.user_id,
                            "target_room": entry.target_room},
            }))
        return {"ok": True, "status": entry.status.value}

    @router.post("/waiting-room/{entry_id}/deny")
    async def deny_entry(entry_id: str, request: Request):
        body = await request.json()
        denied_by = body.get("denied_by", "host")
        reason = body.get("reason", "")
        entry = gov.deny_entry(entry_id, denied_by, reason)
        if not entry:
            raise HTTPException(404, "Entry not found or already resolved")
        return {"ok": True, "status": entry.status.value}

    # ═══ MODERATION ════════════════════════════════════════════════════════

    @router.post("/ban")
    async def ban_user(request: Request):
        body = await request.json()
        user_id = body.get("user_id", "")
        reason = body.get("reason", "No reason given")
        issued_by = body.get("issued_by", "host")
        ban_type = BanType(body.get("ban_type", "session"))
        duration = body.get("duration_hours", None)
        if not user_id:
            raise HTTPException(400, "user_id required")
        ban = gov.ban_user(user_id, reason, issued_by, ban_type, duration)
        if hub:
            import asyncio
            asyncio.create_task(hub.broadcast_system_event({
                "type": "user_banned",
                "payload": {"user_id": user_id, "reason": reason},
            }))
        return {"ok": True, "ban_id": ban.ban_id}

    @router.post("/unban")
    async def unban_user(request: Request):
        body = await request.json()
        user_id = body.get("user_id", "")
        lifted_by = body.get("lifted_by", "host")
        if not gov.unban_user(user_id, lifted_by):
            raise HTTPException(404, "No active ban found")
        return {"ok": True}

    @router.post("/freeze")
    async def freeze_avatar(request: Request):
        body = await request.json()
        user_id = body.get("user_id", "")
        frozen_by = body.get("frozen_by", "host")
        reason = body.get("reason", "")
        pose = body.get("pose_override", None)
        if not user_id:
            raise HTTPException(400, "user_id required")
        state = gov.freeze_avatar(user_id, frozen_by, reason, pose)
        if hub:
            import asyncio
            asyncio.create_task(hub.broadcast_system_event({
                "type": "avatar_frozen",
                "payload": {"user_id": user_id, "frozen_by": frozen_by, "reason": reason},
            }))
        return {"ok": True}

    @router.post("/unfreeze")
    async def unfreeze_avatar(request: Request):
        body = await request.json()
        user_id = body.get("user_id", "")
        unfrozen_by = body.get("unfrozen_by", "host")
        if not gov.unfreeze_avatar(user_id, unfrozen_by):
            raise HTTPException(404, "User not frozen")
        return {"ok": True}

    @router.post("/mute")
    async def mute_user(request: Request):
        body = await request.json()
        user_id = body.get("user_id", "")
        muted_by = body.get("muted_by", "host")
        duration = body.get("duration_seconds", 0)
        if not user_id:
            raise HTTPException(400, "user_id required")
        gov.mute_user(user_id, muted_by, duration)
        return {"ok": True}

    @router.post("/unmute")
    async def unmute_user(request: Request):
        body = await request.json()
        user_id = body.get("user_id", "")
        unmuted_by = body.get("unmuted_by", "host")
        gov.unmute_user(user_id, unmuted_by)
        return {"ok": True}

    @router.post("/kick")
    async def kick_user(request: Request):
        body = await request.json()
        user_id = body.get("user_id", "")
        kicked_by = body.get("kicked_by", "host")
        reason = body.get("reason", "")
        if not user_id:
            raise HTTPException(400, "user_id required")
        gov.ban_user(user_id, reason or "Kicked", kicked_by, BanType.SESSION)
        if hub:
            import asyncio
            asyncio.create_task(hub.broadcast_system_event({
                "type": "user_kicked",
                "payload": {"user_id": user_id, "reason": reason},
            }))
        return {"ok": True}

    # ═══ AUDIT ═════════════════════════════════════════════════════════════

    @router.get("/audit")
    async def get_audit_log(limit: int = 100):
        return gov.get_audit_log(limit)

    @router.get("/audit/{user_id}")
    async def get_user_audit(user_id: str):
        return gov.get_user_history(user_id)

    @router.get("/status")
    async def governance_status():
        return {
            "active_bans": len(gov.list_active_bans()),
            "frozen_avatars": len(gov.list_frozen_avatars()),
            "muted_users": len(gov.list_muted_users()),
            "waiting_room": len(gov.list_waiting()),
        }

    return router
