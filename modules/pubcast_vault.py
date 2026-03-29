"""
modules/pubcast_vault.py — PubCast Vault Integration
═════════════════════════════════════════════════════
Wires the Hardened Iteration Wallet into PubCast as a file
protection layer for recordings, assets, and project versions.

Usage in main.py:
    from modules.pubcast_vault import PubCastVault, create_vault_router
    vault = PubCastVault(DATA_DIR / "vault")
    app.include_router(create_vault_router(vault))

Rear View Foresight LLC — Feic Mo Chroí — 2026-03-24T06:15Z
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from modules.vault_engine_hardened import VaultEngine

logger = logging.getLogger("pubcast.vault")


class PubCastVault:
    """
    PubCast-specific wrapper around the hardened VaultEngine.
    Provides convenience methods for protecting recordings, show cuts,
    and canonical project versions.
    """

    def __init__(self, vault_root: Path) -> None:
        self._engine = VaultEngine(str(vault_root))
        logger.info("PubCast Vault initialized at %s", vault_root)

    @property
    def engine(self) -> VaultEngine:
        return self._engine

    async def protect_recording(self, filepath: str, show_name: str,
                                 version: str = "V1") -> Dict[str, Any]:
        """Protect a finished recording in the vault."""
        self._engine.create_cradle(show_name, str(Path(filepath).parent))
        ok, msg = self._engine.add_file_to_vault(filepath, show_name, version)
        return {"ok": ok, "message": msg}

    async def protect_project_snapshot(self, project_dir: str,
                                        project_name: str,
                                        version: str = "V1") -> Dict[str, Any]:
        """Snapshot an entire project directory into the vault."""
        project = Path(project_dir)
        if not project.is_dir():
            return {"ok": False, "message": f"Not a directory: {project_dir}"}

        self._engine.create_cradle(project_name, project_dir)
        results = []
        for fp in project.rglob("*"):
            if fp.is_file() and not self._engine._is_system_file(fp):
                ok, msg = self._engine.add_file_to_vault(
                    str(fp), project_name, version)
                results.append({"file": fp.name, "ok": ok, "message": msg})

        protected = sum(1 for r in results if r["ok"])
        return {
            "ok": protected > 0,
            "message": f"Protected {protected}/{len(results)} files.",
            "details": results,
        }

    def get_integrity_report(self) -> Dict[str, Any]:
        return self._engine.get_integrity_report()

    def shutdown(self):
        self._engine.stop_watcher()


def create_vault_router(vault: PubCastVault) -> APIRouter:
    """FastAPI router for vault operations."""
    router = APIRouter(prefix="/api/vault", tags=["Vault"])
    engine = vault.engine

    @router.get("/status")
    async def vault_status():
        report = engine.get_integrity_report()
        return {
            "vault_open": engine.is_open(),
            "projects": engine.list_projects(),
            "integrity": report,
            "events_recent": engine.get_event_log(limit=10),
        }

    @router.post("/enforcer/start")
    async def start_enforcer_session(request: Request):
        body = await request.json()
        session_id = body.get("session_id", "")
        if not session_id:
            raise HTTPException(400, "session_id required")
        engine.enforcer.start_session(session_id)
        return {"ok": True, "session_id": session_id}

    @router.post("/enforcer/keystroke")
    async def record_keystroke(request: Request):
        body = await request.json()
        session_id = body.get("session_id", "")
        char = body.get("char", "")
        if not session_id or not char:
            raise HTTPException(400, "session_id and char required")
        ok = engine.enforcer.record_keystroke(session_id, char)
        return {"ok": ok}

    @router.post("/open")
    async def open_vault(request: Request):
        body = await request.json()
        session_id = body.get("session_id", "")
        ok, msg = engine.open_vault(session_id)
        return {"ok": ok, "message": msg}

    @router.post("/lock")
    async def lock_vault():
        ok, msg = engine.lock_vault()
        return {"ok": ok, "message": msg}

    @router.get("/files")
    async def list_files(project: str = None):
        return engine.list_vault_files(project)

    @router.post("/files")
    async def add_file(request: Request):
        body = await request.json()
        source = body.get("source_path", "")
        project = body.get("project_name", "")
        version = body.get("version", "V1")
        if not source or not project:
            raise HTTPException(400, "source_path and project_name required")
        ok, msg = engine.add_file_to_vault(source, project, version)
        return {"ok": ok, "message": msg}

    @router.post("/files/{file_id}/remove")
    async def remove_file(file_id: str, request: Request):
        body = await request.json()
        session_id = body.get("session_id", "")
        ok, msg = engine.remove_file_from_vault(file_id, session_id)
        return {"ok": ok, "message": msg}

    @router.delete("/files/{file_id}")
    async def delete_file(file_id: str, request: Request):
        body = await request.json()
        session_id = body.get("session_id", "")
        ok, msg = engine.delete_file(file_id, session_id)
        return {"ok": ok, "message": msg}

    @router.get("/projects")
    async def list_projects():
        return engine.list_projects()

    @router.post("/projects")
    async def create_project(request: Request):
        body = await request.json()
        name = body.get("name", "").strip()
        root = body.get("root_folder", "").strip()
        if not name:
            raise HTTPException(400, "name required")
        ok, msg = engine.create_cradle(name, root or str(vault.engine.vault_root / name))
        return {"ok": ok, "message": msg}

    @router.post("/projects/{project_name}/promote")
    async def promote(project_name: str, request: Request):
        body = await request.json()
        new_version = body.get("new_version", "")
        if not new_version:
            raise HTTPException(400, "new_version required")
        ok, msg = engine.promote_version(project_name, new_version)
        return {"ok": ok, "message": msg}

    @router.get("/projects/{project_name}/scan")
    async def scan_project(project_name: str):
        return engine.scan_cradle(project_name)

    @router.get("/events")
    async def get_events(limit: int = 100):
        return engine.get_event_log(limit)

    @router.get("/integrity")
    async def integrity_report():
        return engine.get_integrity_report()

    return router
