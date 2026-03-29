"""
modules/byok_routes.py  —  PubCast BYOK FastAPI Routes

Mounts onto the main FastAPI app at /api/byok/...

Endpoints:
  GET  /api/byok/catalog              — full model catalog + hardware profile
  GET  /api/byok/models               — user's registered models
  POST /api/byok/models               — register a new key or local model
  DELETE /api/byok/models/{model_id}  — remove a key
  POST /api/byok/models/{model_id}/test  — run/re-run fidelity test
  GET  /api/byok/hardware             — this machine's hardware profile
  POST /api/byok/character-packet     — build a character packet for guest handoff
  GET  /api/byok/ollama/status        — Ollama running? what models are pulled?

All write endpoints require the user to be authenticated (uses existing PubCast
auth — caller must pass a valid session token that resolves to a user_id).
Keys are never echoed back in any response.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .character_profiles import extra_character_ids

logger = logging.getLogger("pubcast.byok_routes")

router = APIRouter(prefix="/api/byok", tags=["byok"])


# ── Request / response schemas ────────────────────────────────────────────────

class RegisterKeyRequest(BaseModel):
    provider: str = Field(..., description="openai | anthropic | gemini | mistral | groq")
    model_name: str = Field(..., description="Model ID string e.g. 'llama-3.1-8b-instant'")
    api_key: str = Field(..., description="API key — stored encrypted, never returned")
    display_name: str = Field("", description="Human label for this config")
    run_fidelity: bool = Field(True, description="Run character fidelity test on register")


class RegisterOllamaRequest(BaseModel):
    model_name: str = Field(..., description="Ollama model tag e.g. 'ministral:3b'")
    display_name: str = Field("", description="Human label for this config")
    run_fidelity: bool = Field(True, description="Run character fidelity test on register")


class CharacterPacketRequest(BaseModel):
    character_id: str = Field("purfluous", description="purfluous | pete | repeat | jeeves")
    recent_history: List[Dict[str, Any]] = Field(default_factory=list)
    mood_state: str = Field("theatrical")
    current_arousal: float = Field(0.5, ge=0.0, le=1.0)
    custom_system_prompt: str = Field("")


class ModelResponse(BaseModel):
    model_id: str
    provider: str
    display_name: str
    model_name: str
    status: str
    last_verified: Optional[float]
    last_error: Optional[str]
    fidelity_rating: str
    fidelity_verdict: str


class RegisterResponse(BaseModel):
    model_id: str
    status: str
    fidelity_rating: str
    fidelity_verdict: str
    fidelity_score: float
    fidelity_response_preview: str


class FidelityTestResponse(BaseModel):
    model_id: str
    score: float
    rating: str
    verdict: str
    response_preview: str
    elapsed_ms: float


class HardwareResponse(BaseModel):
    ram_gb: float
    vram_gb: float
    cpu_cores: int
    platform: str
    gpu_name: str
    has_gpu: bool
    recommended_model_id: str
    recommended_model_label: str
    viable_model_ids: List[str]


class CatalogEntry(BaseModel):
    id: str
    label: str
    provider: str
    quality: str
    size_gb: float
    min_ram_gb: float
    min_vram_gb: float
    recommended_for: List[str]
    download_url: str
    ollama_pull: str
    bundled: bool
    free: bool
    notes: str
    viable: bool = True       # set by hardware check


class OllamaStatusResponse(BaseModel):
    running: bool
    host: str
    pulled_models: List[str]
    bundled_model_present: bool


# ── Auth dependency ───────────────────────────────────────────────────────────

def _get_current_user_id(request: Request, response: Response) -> str:
    """
    Resolve the current user_id from the PubCast session.

    Matches the existing PubCast auth pattern: reads the pubcast_uid cookie,
    creating and setting a new UUID if not present (same as _get_or_set_uid
    in main.py). This is a session identity, not an access-control gate —
    the same pattern used by all other PubCast routes.
    """
    import uuid as _uuid
    from pathlib import Path as _Path
    uid = request.cookies.get("pubcast_uid")
    if not uid:
        uid = str(_uuid.uuid4())
        response.set_cookie(
            "pubcast_uid", uid,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="lax",
        )
    return uid


def _get_byok(request: Request):
    byok = getattr(request.app.state, "byok_manager", None)
    if byok is None:
        raise HTTPException(status_code=503, detail="BYOK manager not initialised")
    return byok


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/catalog", response_model=List[CatalogEntry])
async def get_catalog(request: Request):
    """
    Return the full model catalog annotated with hardware viability.
    Does not require authentication — public endpoint.
    """
    from .llm_framework import MODEL_CATALOG

    byok = getattr(request.app.state, "byok_manager", None)
    viable_ids = set()
    if byok:
        for spec in byok.viable_local_models():
            viable_ids.add(spec.id)

    return [
        CatalogEntry(
            id=m.id,
            label=m.label,
            provider=m.provider,
            quality=m.quality,
            size_gb=m.size_gb,
            min_ram_gb=m.min_ram_gb,
            min_vram_gb=m.min_vram_gb,
            recommended_for=m.recommended_for,
            download_url=m.download_url,
            ollama_pull=m.ollama_pull,
            bundled=m.bundled,
            free=m.free,
            notes=m.notes,
            viable=m.provider != "ollama" or m.id in viable_ids,
        )
        for m in MODEL_CATALOG
    ]


@router.get("/hardware", response_model=HardwareResponse)
async def get_hardware(request: Request):
    """Return this machine's hardware profile and recommended model."""
    byok = _get_byok(request)
    hw = byok.hardware_profile()
    if hw is None:
        raise HTTPException(status_code=503, detail="Hardware probe not yet run")
    rec = byok.recommended_local_model()
    viable = byok.viable_local_models()
    return HardwareResponse(
        ram_gb=hw.ram_gb,
        vram_gb=hw.vram_gb,
        cpu_cores=hw.cpu_cores,
        platform=hw.platform_name,
        gpu_name=hw.gpu_name,
        has_gpu=hw.has_gpu,
        recommended_model_id=rec.id,
        recommended_model_label=rec.label,
        viable_model_ids=[m.id for m in viable],
    )


@router.get("/ollama/status", response_model=OllamaStatusResponse)
async def get_ollama_status(request: Request):
    """Check if Ollama is running and what models are available."""
    from .llm_framework import OllamaBotAdapter, PROVIDER_REGISTRY
    import os
    # Use the registry instance (configured at startup) rather than creating a new one.
    adapter = PROVIDER_REGISTRY.get("ollama")
    if not isinstance(adapter, OllamaBotAdapter):
        # Fallback: create one with current env
        adapter = OllamaBotAdapter(host=os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    host = adapter.host
    running = await adapter.ping()
    pulled: List[str] = []
    if running:
        pulled = await adapter.list_local_models()
    bundled_present = any("gemma3:1b" in m for m in pulled)
    return OllamaStatusResponse(
        running=running,
        host=host,
        pulled_models=pulled,
        bundled_model_present=bundled_present,
    )


@router.get("/models", response_model=List[ModelResponse])
async def list_models(
    request: Request,
    user_id: str = Depends(_get_current_user_id),
):
    """List all LLM configurations registered by the current user."""
    byok = _get_byok(request)
    return byok.list_user_models(user_id)


@router.post("/models", response_model=RegisterResponse, status_code=201)
async def register_key(
    payload: RegisterKeyRequest,
    request: Request,
    user_id: str = Depends(_get_current_user_id),
):
    """
    Register a cloud API key for the current user.
    The key is encrypted at rest and never returned by any endpoint.
    """
    byok = _get_byok(request)

    VALID_PROVIDERS = {"openai", "anthropic", "gemini", "mistral", "groq"}
    if payload.provider not in VALID_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider '{payload.provider}'. Valid: {sorted(VALID_PROVIDERS)}",
        )
    if not payload.api_key.strip():
        raise HTTPException(status_code=400, detail="api_key must not be empty")
    if not payload.model_name.strip():
        raise HTTPException(status_code=400, detail="model_name must not be empty")

    try:
        model_id = await byok.register_key(
            user_id=user_id,
            provider=payload.provider,
            model_name=payload.model_name,
            api_key=payload.api_key,
            display_name=payload.display_name,
            run_fidelity=payload.run_fidelity,
        )
    except Exception as exc:
        logger.exception("register_key failed for user %s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to register key")

    # Fetch fidelity result via public accessor
    fidelity = byok.get_session_fidelity(user_id, model_id)
    rating = "untested"
    verdict = ""
    score = 0.0
    preview = ""
    if fidelity:
        r = fidelity.result
        rating = r.rating
        verdict = r.verdict
        score = round(r.score, 3)
        preview = r.response[:120] if r.response else ""

    return RegisterResponse(
        model_id=model_id,
        status="ready" if rating != "broken" else "error",
        fidelity_rating=rating,
        fidelity_verdict=verdict,
        fidelity_score=score,
        fidelity_response_preview=preview,
    )


@router.post("/models/ollama", response_model=RegisterResponse, status_code=201)
async def register_ollama(
    payload: RegisterOllamaRequest,
    request: Request,
    user_id: str = Depends(_get_current_user_id),
):
    """Register a local Ollama model (no API key required)."""
    byok = _get_byok(request)
    if not payload.model_name.strip():
        raise HTTPException(status_code=400, detail="model_name must not be empty")

    try:
        model_id = await byok.register_ollama_model(
            user_id=user_id,
            model_name=payload.model_name,
            display_name=payload.display_name,
            run_fidelity=payload.run_fidelity,
        )
    except Exception as exc:
        logger.exception("register_ollama failed for user %s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to register Ollama model")

    fidelity = byok.get_session_fidelity(user_id, model_id)
    rating = "untested"
    verdict = ""
    score = 0.0
    preview = ""
    if fidelity:
        r = fidelity.result
        rating = r.rating
        verdict = r.verdict
        score = round(r.score, 3)
        preview = r.response[:120] if r.response else ""

    return RegisterResponse(
        model_id=model_id,
        status="ready" if rating != "broken" else "error",
        fidelity_rating=rating,
        fidelity_verdict=verdict,
        fidelity_score=score,
        fidelity_response_preview=preview,
    )


@router.delete("/models/{model_id}", status_code=204)
async def remove_model(
    model_id: str,
    request: Request,
    user_id: str = Depends(_get_current_user_id),
):
    """Remove a registered model and delete its stored key."""
    byok = _get_byok(request)
    if not byok.model_exists(user_id, model_id):
        raise HTTPException(status_code=404, detail="Model not found")
    await byok.remove_key(user_id, model_id)


@router.post("/models/{model_id}/test", response_model=FidelityTestResponse)
async def run_fidelity_test(
    model_id: str,
    request: Request,
    user_id: str = Depends(_get_current_user_id),
):
    """Run (or re-run) the character fidelity test for a registered model."""
    byok = _get_byok(request)
    result = await byok.run_fidelity_test(user_id, model_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Model not found")
    return FidelityTestResponse(
        model_id=model_id,
        score=round(result.score, 3),
        rating=result.rating,
        verdict=result.verdict,
        response_preview=result.response[:200] if result.response else "",
        elapsed_ms=round(result.elapsed_ms, 1),
    )


@router.post("/character-packet")
async def get_character_packet(
    payload: CharacterPacketRequest,
    request: Request,
    user_id: str = Depends(_get_current_user_id),
):
    """
    Build a character state packet for guest BYOK handoff.
    The guest's model uses this to proxy the character for their conversation leg.
    """
    byok = _get_byok(request)
    VALID_CHARACTERS = {"purfluous", "jeeves", *extra_character_ids()}
    if payload.character_id not in VALID_CHARACTERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown character '{payload.character_id}'. Valid: {sorted(VALID_CHARACTERS)}",
        )
    packet = byok.build_character_packet(
        character_id=payload.character_id,
        recent_history=payload.recent_history,
        mood_state=payload.mood_state,
        current_arousal=payload.current_arousal,
        custom_system_prompt=payload.custom_system_prompt,
    )
    return packet.to_dict()


# ── Mount helper ──────────────────────────────────────────────────────────────

def mount_byok_routes(app: Any, byok_manager: Any) -> None:
    """
    Call this from main.py after creating the FastAPI app and BYOKManager.

    Example:
        from modules.byok_manager import BYOKManager
        from modules.byok_routes import mount_byok_routes

        byok = BYOKManager(data_dir=Path("data"))
        app.state.byok_manager = byok
        mount_byok_routes(app, byok)
    """
    app.state.byok_manager = byok_manager
    app.include_router(router)
    logger.info("BYOK routes mounted at /api/byok/...")


__all__ = ["router", "mount_byok_routes"]
