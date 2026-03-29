"""
modules/production_routes.py — Camera + Recording REST API
═══════════════════════════════════════════════════════════
Wires CameraManager and RecordingService into FastAPI.
Also provides WebSocket broadcast helpers so the control room
console receives live updates on camera switches and recording state.

Usage in main.py:
    from modules.cameras import create_default_cameras
    from modules.recording import create_recording_service
    from modules.production_routes import create_production_router

    cameras = create_default_cameras()
    recording = create_recording_service(DATA_DIR, cameras)
    app.include_router(create_production_router(cameras, recording, hub))

Rear View Foresight LLC — Feic Mo Chroí — 2026-03-24T08:00Z
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse

from modules.cameras import CameraManager, CameraSource, CameraTransport, create_default_cameras
from modules.recording import (
    RecordingService,
    RecordingSession,
    RecordingState,
    ContainerFormat,
    EncodingProfile,
    create_recording_service,
)

logger = logging.getLogger("pubcast.production")


def create_production_router(
    cameras: CameraManager,
    recording: RecordingService,
    hub: Any,
) -> APIRouter:
    """Create the production API router."""
    router = APIRouter(tags=["Production"])

    # ═══════════════════════════════════════════════════════════════════
    # CAMERAS
    # ═══════════════════════════════════════════════════════════════════

    @router.get("/api/cameras")
    async def list_cameras():
        """List all registered camera sources with status."""
        sources = cameras.list_sources()
        statuses = {s.source_id: s for s in cameras.list_status()}
        pgm = cameras.get_program_source()
        pvw = cameras.get_preview_source()

        return {
            "cameras": [
                {
                    "source_id": cam.source_id,
                    "name": cam.name,
                    "location": cam.location,
                    "transport": cam.transport.value,
                    "endpoint": cam.endpoint,
                    "position": cam.position,
                    "rotation": cam.rotation,
                    "fov": cam.fov,
                    "tags": cam.tags,
                    "status": {
                        "online": statuses.get(cam.source_id, None)
                                  and statuses[cam.source_id].online,
                        "latency_ms": getattr(statuses.get(cam.source_id), "latency_ms", None),
                        "frame_rate": getattr(statuses.get(cam.source_id), "frame_rate", None),
                    },
                }
                for cam in sources
            ],
            "program": pgm.source_id if pgm else None,
            "preview": pvw.source_id if pvw else None,
        }

    @router.post("/api/cameras/switch")
    async def switch_camera(request: Request):
        """Switch program or preview camera. Broadcasts to all control rooms."""
        body = await request.json()
        target = body.get("target", "program")  # "program" or "preview"
        source_id = body.get("source_id", "")

        if target == "program":
            ok = cameras.set_program_source(source_id)
        elif target == "preview":
            ok = cameras.set_preview_source(source_id)
        else:
            raise HTTPException(400, f"Invalid target '{target}'. Use 'program' or 'preview'.")

        if not ok:
            raise HTTPException(404, f"Camera source '{source_id}' not found.")

        # Broadcast switch to all connected clients
        if hub:
            pgm = cameras.get_program_source()
            pvw = cameras.get_preview_source()
            await hub.broadcast_system_event({
                "type": "camera_switch",
                "payload": {
                    "target": target,
                    "source_id": source_id,
                    "program": pgm.source_id if pgm else None,
                    "preview": pvw.source_id if pvw else None,
                    "timestamp": time.time(),
                },
            })

        return {"ok": True, "target": target, "source_id": source_id}

    @router.post("/api/cameras/cut")
    async def cut_cameras():
        """Swap program and preview (standard broadcast CUT transition)."""
        pgm = cameras.get_program_source()
        pvw = cameras.get_preview_source()

        if not pgm or not pvw:
            raise HTTPException(400, "Both program and preview must be set to cut.")

        cameras.set_program_source(pvw.source_id)
        cameras.set_preview_source(pgm.source_id)

        if hub:
            await hub.broadcast_system_event({
                "type": "camera_cut",
                "payload": {
                    "new_program": pvw.source_id,
                    "new_preview": pgm.source_id,
                    "timestamp": time.time(),
                },
            })

        return {"ok": True, "program": pvw.source_id, "preview": pgm.source_id}

    @router.post("/api/cameras/register")
    async def register_camera(request: Request):
        """Register a new camera source."""
        body = await request.json()
        try:
            source = CameraSource(
                source_id=body["source_id"],
                name=body.get("name", body["source_id"]),
                location=body.get("location", "virtual"),
                description=body.get("description", ""),
                transport=CameraTransport(body.get("transport", "virtual")),
                endpoint=body.get("endpoint", f"virtual://{body['source_id']}"),
                position=body.get("position", [0.0, 0.0, 0.0]),
                rotation=body.get("rotation", [0.0, 0.0, 0.0]),
                fov=body.get("fov", 60.0),
                tags=body.get("tags", []),
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc))

        cameras.register(source)
        return {"ok": True, "source_id": source.source_id}

    # ═══════════════════════════════════════════════════════════════════
    # RECORDING
    # ═══════════════════════════════════════════════════════════════════

    @router.get("/api/recording/profiles")
    async def list_profiles():
        """List available encoding profiles."""
        return [
            {
                "profile_id": p.profile_id,
                "name": p.name,
                "container": p.container.value,
                "video_codec": p.video_codec,
                "audio_codec": p.audio_codec,
                "resolution": p.resolution,
                "description": p.description,
            }
            for p in recording.list_profiles()
        ]

    @router.get("/api/recording/sessions")
    async def list_sessions():
        """List all recording sessions."""
        return [s.to_dict() for s in recording.list_sessions()]

    @router.post("/api/recording/start")
    async def start_recording(request: Request):
        """Start a new recording session."""
        body = await request.json()
        sources = body.get("sources", [])
        if not sources:
            # Default: record the current program source
            pgm = cameras.get_program_source()
            if pgm:
                sources = [pgm.source_id]
            else:
                raise HTTPException(400, "No sources specified and no program source set.")

        profile_id = body.get("profile_id", "broadcast_mp4")
        operator = body.get("operator", "director")
        session_id = body.get("session_id", None)
        countdown = body.get("countdown_seconds", 5)

        try:
            session = recording.start_session(
                session_id=session_id,
                sources=sources,
                profile_id=profile_id,
                operator=operator,
                countdown_seconds=countdown,
                host_override=body.get("host_override", False),
            )
        except (ValueError, PermissionError, KeyError) as exc:
            raise HTTPException(400, str(exc))

        # Broadcast recording start
        if hub:
            await hub.broadcast_system_event({
                "type": "recording_started",
                "payload": {
                    "session_id": session.session_id,
                    "sources": session.sources,
                    "profile_id": session.profile_id,
                    "state": session.state.value,
                    "countdown": countdown,
                },
            })

        return session.to_dict()

    @router.post("/api/recording/{session_id}/activate")
    async def activate_recording(session_id: str):
        """Activate a recording session (transition from countdown to active)."""
        try:
            session = recording.activate_session(session_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc))

        if hub:
            await hub.broadcast_system_event({
                "type": "recording_active",
                "payload": {"session_id": session_id},
            })

        return session.to_dict()

    @router.post("/api/recording/{session_id}/stop")
    async def stop_recording(session_id: str):
        """Stop a recording session."""
        try:
            session = recording.stop_session(session_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc))

        if hub:
            await hub.broadcast_system_event({
                "type": "recording_stopped",
                "payload": {
                    "session_id": session_id,
                    "duration": (session.ended_at or time.time()) - session.started_at,
                },
            })

        return session.to_dict()

    @router.post("/api/recording/{session_id}/pause")
    async def pause_recording(session_id: str, request: Request):
        """Pause or resume a recording session."""
        body = await request.json()
        paused = body.get("paused", True)
        try:
            session = recording.pause_session(session_id, paused)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        return session.to_dict()

    @router.post("/api/recording/{session_id}/marker")
    async def add_marker(session_id: str, request: Request):
        """Add a timestamp marker to a recording session."""
        body = await request.json()
        label = body.get("label", "")
        operator = body.get("operator", None)
        if not label:
            raise HTTPException(400, "Marker label required.")
        try:
            marker = recording.mark_moment(session_id, label, operator)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        return {"ok": True, "timestamp": marker.timestamp, "label": marker.label}

    @router.post("/api/recording/{session_id}/export")
    async def export_recording(session_id: str, request: Request):
        """Export a recording session as a zip bundle."""
        body = await request.json()
        container = body.get("format", "mp4")
        try:
            fmt = ContainerFormat(container)
        except ValueError:
            raise HTTPException(400, f"Invalid format '{container}'. Use: mp4, webm, wav, mov")

        try:
            export_path = await asyncio.to_thread(
                recording.export_session, session_id, container=fmt
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc))

        return FileResponse(
            export_path,
            media_type="application/zip",
            filename=export_path.name,
        )

    @router.post("/api/recording/import")
    async def import_recording(file: UploadFile = File(...)):
        """Import a recording session from a zip bundle."""
        import_dir = recording.imports_dir
        import_path = import_dir / file.filename
        with open(import_path, "wb") as f:
            content = await file.read()
            f.write(content)
        try:
            session = recording.import_session(import_path)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(400, str(exc))
        return session.to_dict()

    @router.get("/api/recording/storage")
    async def storage_status():
        """Get recording storage usage."""
        return recording.storage_status()

    @router.get("/api/recording/privacy")
    async def privacy_matrix():
        """Get recording privacy rules per room."""
        return recording.privacy_matrix()

    # ═══════════════════════════════════════════════════════════════════
    # PANIC / Emergency
    # ═══════════════════════════════════════════════════════════════════

    @router.post("/api/production/panic")
    async def production_panic():
        """Emergency: stop all recordings, mute, fade to black."""
        # Stop all active recordings
        stopped = []
        for session in recording.list_sessions():
            if session.state in (RecordingState.ACTIVE, RecordingState.COUNTDOWN):
                recording.stop_session(session.session_id)
                stopped.append(session.session_id)

        # Broadcast panic to all clients
        if hub:
            await hub.broadcast_system_event({
                "type": "production_panic",
                "payload": {
                    "timestamp": time.time(),
                    "sessions_stopped": stopped,
                    "message": "PANIC — all recordings stopped, fade to black",
                },
            })
            # Also update production state
            hub.update_production_state({
                "on_air": False,
                "camera": "black",
                "lower_third": "",
            })

        return {"ok": True, "sessions_stopped": stopped}

    return router
