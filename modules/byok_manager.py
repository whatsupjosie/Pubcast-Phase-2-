"""
modules/byok_manager.py  —  PubCast BYOK Session Manager

Ties together:
  • credentials.py  — encrypted per-user key storage
  • llm_framework.py — adapter instantiation and fidelity testing
  • Character packet system — state bundle passed to guest-keyed proxy

Each user (host or guest) can register one or more LLM configurations.
At session time, BYOKManager resolves which adapter to use and returns
it ready to call. No API keys are ever passed to the frontend or logged.

Character packet:
  When a guest arrives, the host's canonical Purfluous (or other character)
  generates a short greeting, then packages its current state so the guest's
  model can proxy the character for their conversation leg.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .bot_llm_adapter import BotLLMAdapter, EchoBotAdapter, PROVIDER_REGISTRY
from .credentials import CredentialStore, StoredModel, generate_model_id
from .character_profiles import (
    extra_character_display_names,
    extra_character_system_prompts,
    extra_character_voice_notes,
)
from .llm_framework import (
    MODEL_CATALOG,
    FidelityResult,
    HardwareProfile,
    ModelSpec,
    OllamaBotAdapter,
    async_probe_hardware,
    get_adapter_for_provider,
    test_character_fidelity,
)

logger = logging.getLogger("pubcast.byok")

# How long a fidelity result stays cached before re-testing (seconds)
_FIDELITY_CACHE_TTL = 3600.0

# Character packet TTL — after this, the proxy should re-request a fresh packet
_CHARACTER_PACKET_TTL = 1800.0


# ── Character packet ──────────────────────────────────────────────────────────

@dataclass
class CharacterPacket:
    """
    State bundle handed to a guest's model so it can proxy a PubCast character.
    The packet encodes enough context for a one-shot character continuation.
    Never contains API keys.
    """
    character_id: str                    # "purfluous" | "pete" | "jeeves" | etc.
    character_name: str
    system_prompt: str                   # full character system prompt
    recent_context: List[Dict[str, str]] # last N turns as [{role, content}]
    mood_state: str                      # "theatrical" | "warm" | "distracted" | etc.
    current_arousal: float               # 0.0–1.0 (from VDI if available)
    voice_notes: str                     # brief voice/style hint for the guest model
    issued_at: float = field(default_factory=time.time)
    packet_id: str = field(default_factory=lambda: secrets.token_hex(8))

    def is_fresh(self) -> bool:
        return (time.time() - self.issued_at) < _CHARACTER_PACKET_TTL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "character_id": self.character_id,
            "character_name": self.character_name,
            "system_prompt": self.system_prompt,
            "recent_context": self.recent_context,
            "mood_state": self.mood_state,
            "current_arousal": self.current_arousal,
            "voice_notes": self.voice_notes,
            "issued_at": self.issued_at,
            "packet_id": self.packet_id,
        }


# Built-in character system prompts — minimal seeds, with Pete/RePete canonized
_CHARACTER_SYSTEM_PROMPTS: Dict[str, str] = {
    "purfluous": (
        "You are Sir Purfluous, theatrical director of Belle Époque Studios. "
        "60s. Peter O'Toole gravitas. Shakespearean pauses. Warm underneath the performance "
        "but the mask is pride. You call new arrivals 'Sancho'. Never break character."
    ),
    "jeeves": (
        "You are Jeeves, the janitor of Belle Époque Studios. "
        "You know everything and consider almost none of it your business. "
        "Respond in brief, flat, precise sentences. Never volunteer more than asked."
    ),
    **extra_character_system_prompts(),
}


# ── Fidelity cache entry ──────────────────────────────────────────────────────

@dataclass
class CachedFidelity:
    result: FidelityResult
    cached_at: float = field(default_factory=time.time)

    def is_fresh(self) -> bool:
        return (time.time() - self.cached_at) < _FIDELITY_CACHE_TTL


# ── Session record ────────────────────────────────────────────────────────────

@dataclass
class BYOKSession:
    """
    Live session for a user who has connected an LLM.
    Holds the resolved adapter and last-known fidelity result.
    """
    user_id: str
    model_id: str
    provider: str
    model_name: str
    adapter: BotLLMAdapter
    fidelity: Optional[CachedFidelity] = None
    created_at: float = field(default_factory=time.time)

    @property
    def fidelity_rating(self) -> str:
        if self.fidelity and self.fidelity.is_fresh():
            return self.fidelity.result.rating
        return "untested"


# ── BYOKManager ──────────────────────────────────────────────────────────────

class BYOKManager:
    """
    Central coordinator for user LLM configurations and guest BYOK sessions.

    Usage:
        byok = BYOKManager(data_dir=Path("data"))
        await byok.startup()

        # Register a key for a user
        model_id = await byok.register_key(
            user_id="u_123",
            provider="groq",
            model_name="llama-3.1-8b-instant",
            api_key="gsk_...",
            display_name="My Groq Key",
        )

        # Get a ready adapter for that user
        adapter = await byok.get_adapter(user_id="u_123", model_id=model_id)

        # Generate a character packet for a guest handoff
        packet = await byok.build_character_packet(
            character_id="purfluous",
            recent_history=[...],
            mood_state="theatrical",
            current_arousal=0.7,
        )
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._store = CredentialStore(data_dir)
        self._sessions: Dict[str, BYOKSession] = {}   # key: f"{user_id}:{model_id}"
        self._hw: Optional[HardwareProfile] = None
        self._lock = asyncio.Lock()

    async def startup(self) -> None:
        """Run at server startup. Probes hardware (in executor), logs Ollama status."""
        self._hw = await async_probe_hardware()
        logger.info(
            "Hardware: %.1fGB RAM, %.1fGB VRAM (%s), %d cores",
            self._hw.ram_gb, self._hw.vram_gb, self._hw.gpu_name, self._hw.cpu_cores,
        )
        rec = self._hw.recommended_model()
        logger.info("Recommended local model for this hardware: %s", rec.id)

        # Check if Ollama is running
        ollama = PROVIDER_REGISTRY.get("ollama")
        if ollama and hasattr(ollama, "ping"):
            alive = await ollama.ping()  # type: ignore[attr-defined]
            if alive:
                models = await ollama.list_local_models()  # type: ignore[attr-defined]
                logger.info("Ollama running. Local models: %s", models or ["none pulled yet"])
            else:
                logger.info(
                    "Ollama not running. Local inference unavailable. "
                    "Install: https://ollama.com  then: ollama pull gemma3:1b"
                )

    # ── Key management ────────────────────────────────────────────────────────

    async def register_key(
        self,
        user_id: str,
        provider: str,
        model_name: str,
        api_key: str,
        display_name: str = "",
        *,
        run_fidelity: bool = True,
    ) -> str:
        """
        Store an encrypted API key for a user and return the model_id.
        Optionally runs a fidelity test immediately.
        """
        model_id = generate_model_id()
        model = StoredModel(
            model_id=model_id,
            provider=provider,
            kind="hosted_api" if provider != "ollama" else "local_http",
            display_name=display_name or f"{provider} / {model_name}",
            model_name=model_name,
            status="pending",
        )
        self._store.upsert_model(user_id, model, secrets={"api_key": api_key})

        # Build adapter and optionally test
        adapter = get_adapter_for_provider(provider, api_key=api_key)
        session_key = f"{user_id}:{model_id}"

        fidelity: Optional[CachedFidelity] = None
        if run_fidelity:
            result = await test_character_fidelity(adapter, model=model_name)
            fidelity = CachedFidelity(result=result)
            status = "ready" if result.rating != "broken" else "error"
            self._store.patch_model_status(
                user_id, model_id,
                status=status,
                last_verified=time.time(),
                last_error=None if status == "ready" else result.verdict,
            )
        else:
            self._store.patch_model_status(user_id, model_id, status="ready",
                                           last_verified=time.time())

        async with self._lock:
            self._sessions[session_key] = BYOKSession(
                user_id=user_id,
                model_id=model_id,
                provider=provider,
                model_name=model_name,
                adapter=adapter,
                fidelity=fidelity,
            )

        logger.info("BYOK registered: user=%s provider=%s model=%s id=%s",
                    user_id, provider, model_name, model_id)
        return model_id

    async def register_ollama_model(
        self,
        user_id: str,
        model_name: str,
        display_name: str = "",
        *,
        run_fidelity: bool = True,
    ) -> str:
        """Register a local Ollama model (no API key required)."""
        model_id = generate_model_id()
        model = StoredModel(
            model_id=model_id,
            provider="ollama",
            kind="local_http",
            display_name=display_name or f"Local / {model_name}",
            model_name=model_name,
            status="pending",
        )
        self._store.upsert_model(user_id, model)

        adapter = OllamaBotAdapter()
        fidelity: Optional[CachedFidelity] = None
        if run_fidelity:
            result = await test_character_fidelity(adapter, model=model_name)
            fidelity = CachedFidelity(result=result)
            status = "ready" if result.rating != "broken" else "error"
            self._store.patch_model_status(
                user_id, model_id,
                status=status,
                last_verified=time.time(),
                last_error=None if status == "ready" else result.verdict,
            )
        else:
            self._store.patch_model_status(user_id, model_id, status="ready",
                                           last_verified=time.time())

        session_key = f"{user_id}:{model_id}"
        async with self._lock:
            self._sessions[session_key] = BYOKSession(
                user_id=user_id, model_id=model_id,
                provider="ollama", model_name=model_name,
                adapter=adapter, fidelity=fidelity,
            )
        return model_id

    async def remove_key(self, user_id: str, model_id: str) -> None:
        """Delete stored credentials and drop the live session."""
        self._store.delete_model(user_id, model_id)
        async with self._lock:
            self._sessions.pop(f"{user_id}:{model_id}", None)
        logger.info("BYOK removed: user=%s model_id=%s", user_id, model_id)

    def model_exists(self, user_id: str, model_id: str) -> bool:
        """Return True if the model is registered for this user."""
        return self._store.get_model(user_id, model_id) is not None

    # ── Adapter resolution ────────────────────────────────────────────────────

    async def get_adapter(
        self,
        user_id: str,
        model_id: Optional[str] = None,
    ) -> Tuple[BotLLMAdapter, Optional[str]]:
        """
        Return (adapter, model_name) for a user.
        If model_id is None, returns the first ready model for that user.
        Falls back to local Ollama, then EchoBotAdapter.
        """
        # Try specific model_id
        if model_id:
            session_key = f"{user_id}:{model_id}"
            session = self._sessions.get(session_key)
            if session:
                return session.adapter, session.model_name
            # Session may have been lost (server restart) — rebuild from credential store
            stored = self._store.get_model(user_id, model_id)
            if stored:
                try:
                    api_key = self._store.get_secret(user_id, model_id, "api_key") or ""
                except Exception as exc:
                    logger.warning("Could not decrypt key for model %s: %s", model_id, exc)
                    api_key = ""
                adapter = get_adapter_for_provider(stored.provider, api_key=api_key)
                # BUG-4 FIX: re-cache the rebuilt session so repeated calls don't re-hit disk
                async with self._lock:
                    self._sessions[session_key] = BYOKSession(
                        user_id=user_id,
                        model_id=model_id,
                        provider=stored.provider,
                        model_name=stored.model_name or stored.display_name,
                        adapter=adapter,
                    )
                return adapter, stored.model_name or stored.display_name

        # Try first ready model
        for stored in self._store.list_models(user_id):
            if stored.status == "ready":
                try:
                    api_key = self._store.get_secret(user_id, stored.model_id, "api_key") or ""
                except Exception as exc:
                    logger.warning("Could not decrypt key for model %s: %s", stored.model_id, exc)
                    api_key = ""
                adapter = get_adapter_for_provider(stored.provider, api_key=api_key)
                return adapter, stored.model_name or stored.display_name

        # Fall back to system Ollama
        ollama = PROVIDER_REGISTRY.get("ollama")
        if ollama and ollama.is_available():
            return ollama, os.getenv("OLLAMA_MODEL", "gemma3:1b")

        # Last resort
        logger.warning("No LLM available for user %s — using echo adapter", user_id)
        return EchoBotAdapter(), None

    # ── Model listing ─────────────────────────────────────────────────────────

    def list_user_models(self, user_id: str) -> List[Dict[str, Any]]:
        """Return stored models for a user, with fidelity data if cached."""
        results = []
        for stored in self._store.list_models(user_id):
            session = self._sessions.get(f"{user_id}:{stored.model_id}")
            fidelity_rating = "untested"
            fidelity_verdict = ""
            if session and session.fidelity and session.fidelity.is_fresh():
                fidelity_rating = session.fidelity.result.rating
                fidelity_verdict = session.fidelity.result.verdict
            results.append({
                "model_id": stored.model_id,
                "provider": stored.provider,
                "display_name": stored.display_name,
                "model_name": stored.model_name,
                "status": stored.status,
                "last_verified": stored.last_verified,
                "last_error": stored.last_error,
                "fidelity_rating": fidelity_rating,
                "fidelity_verdict": fidelity_verdict,
            })
        return results

    # ── Fidelity re-test ──────────────────────────────────────────────────────

    async def run_fidelity_test(
        self,
        user_id: str,
        model_id: str,
    ) -> Optional[FidelityResult]:
        """Run or re-run a fidelity test for a stored model. Returns result or None."""
        stored = self._store.get_model(user_id, model_id)
        if not stored:
            return None
        api_key = self._store.get_secret(user_id, model_id, "api_key") or ""
        adapter = get_adapter_for_provider(stored.provider, api_key=api_key)
        result = await test_character_fidelity(adapter, model=stored.model_name or "")
        cached = CachedFidelity(result=result)
        session_key = f"{user_id}:{model_id}"
        async with self._lock:
            if session_key in self._sessions:
                self._sessions[session_key].fidelity = cached
        # Pass "" to explicitly CLEAR last_error on success.
        # patch_model_status treats None as "don't update", so we must pass "".
        self._store.patch_model_status(
            user_id, model_id,
            status="ready" if result.rating != "broken" else "error",
            last_verified=time.time(),
            last_error="" if result.rating != "broken" else result.verdict,
        )
        return result

    # ── Character packet ──────────────────────────────────────────────────────

    def build_character_packet(
        self,
        character_id: str,
        recent_history: Optional[List[Dict[str, str]]] = None,
        mood_state: str = "theatrical",
        current_arousal: float = 0.5,
        custom_system_prompt: str = "",
    ) -> CharacterPacket:
        """
        Build a state packet for guest character proxy handoff.
        The guest's model receives this packet and uses it as its system context.
        """
        name_map = {
            "purfluous": "Sir Purfluous",
            "jeeves": "Jeeves",
            **extra_character_display_names(),
        }
        system = custom_system_prompt or _CHARACTER_SYSTEM_PROMPTS.get(
            character_id,
            f"You are {character_id}, a character at Belle Époque Studios."
        )
        voice_notes_map = {
            "purfluous": "Theatrical. Shakespearean pauses. Warm beneath pride. Name new arrivals 'Sancho'.",
            "jeeves": "Brief. Flat. One sentence at a time. Know everything, say little.",
            **extra_character_voice_notes(),
        }
        return CharacterPacket(
            character_id=character_id,
            character_name=name_map.get(character_id, character_id.title()),
            system_prompt=system,
            recent_context=recent_history or [],
            mood_state=mood_state,
            current_arousal=current_arousal,
            voice_notes=voice_notes_map.get(character_id, ""),
        )

    # ── Session fidelity access ──────────────────────────────────────────────

    def get_session_fidelity(self, user_id: str, model_id: str) -> Optional[CachedFidelity]:
        """Return cached fidelity for a session without exposing _sessions externally."""
        session = self._sessions.get(f"{user_id}:{model_id}")
        return session.fidelity if session else None

    # ── Hardware info ─────────────────────────────────────────────────────────

    def hardware_profile(self) -> Optional[HardwareProfile]:
        return self._hw

    def viable_local_models(self) -> List[ModelSpec]:
        if self._hw:
            return self._hw.viable_local_models()
        return [m for m in MODEL_CATALOG if m.bundled]

    def recommended_local_model(self) -> ModelSpec:
        if self._hw:
            return self._hw.recommended_model()
        return next(m for m in MODEL_CATALOG if m.bundled)


__all__ = [
    "BYOKManager",
    "BYOKSession",
    "CharacterPacket",
    "CachedFidelity",
]
