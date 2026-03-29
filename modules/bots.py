from __future__ import annotations

import asyncio
import threading
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from pydantic import ValidationError

from .appconfig import settings
from .models import BotConfig, BotProvider, UserState
from .ollama_provider import call_ollama, resolve_ollama_model
from .persistence import read_json, write_json

if False:  # typing helpers without import cycle
    from modules.hub import Hub  # pragma: no cover

logger = logging.getLogger(__name__)


@dataclass
class BotSession:
    config: BotConfig
    busy: bool = False
    last_reply_at: float = field(default=0.0)
    _pending_task: object = field(default=None, repr=False, compare=False)


class BotManager:
    """
    Coordinates AI co-hosts (ChatGPT, Gemini, etc.) so they can participate in rooms.
    Bots are virtual users that speak when their owner is present and the conversation calls for them.
    """

    def __init__(self, data_dir: Path, hub: "Hub", byok_mgr: Any = None):
        self.data_dir = data_dir
        self.hub = hub
        self._byok_mgr = byok_mgr   # injected after startup; may be None initially
        self._cricket_keeper = None
        self.config_dir = self.data_dir / "bots"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self._configs: Dict[str, BotConfig] = {}
        self._sessions: Dict[str, BotSession] = {}
        self._lock = asyncio.Lock()
        self._config_lock = threading.Lock()
        self.reload()

    def set_byok_manager(self, byok_mgr: Any) -> None:
        """Wire in the BYOK manager after lifespan startup completes."""
        self._byok_mgr = byok_mgr

    def set_cricket_keeper(self, cricket_keeper: Any) -> None:
        """Optional Jeremy Cricket registry for per-bot memory."""
        self._cricket_keeper = cricket_keeper

    # ---------- lifecycle ----------
    def reload(self) -> None:
        with self._config_lock:
            self._reload_inner()

    def _reload_inner(self) -> None:
        self._configs.clear()
        self._sessions.clear()
        for path in self.config_dir.glob("*.json"):
            data = read_json(path)
            if not data:
                continue
            try:
                cfg = BotConfig(**data)
            except ValidationError as exc:
                logger.error("Invalid bot config at %s: %s", path, exc)
                continue
            self._configs[cfg.bot_id] = cfg
            self._sessions[cfg.bot_id] = BotSession(config=cfg)
            self._ensure_bot_profile(cfg)
        logger.info("Loaded %d bot configurations.", len(self._configs))

    def list_configs(self) -> List[BotConfig]:
        return list(self._configs.values())

    def get_config(self, bot_id: str) -> Optional[BotConfig]:
        return self._configs.get(bot_id)

    def upsert_config(self, config: BotConfig) -> None:
        with self._config_lock:
            self._upsert_inner(config)

    def _upsert_inner(self, config: BotConfig) -> None:
        existing = self._configs.get(config.bot_id)
        if existing and existing.owner_id != config.owner_id:
            raise PermissionError("Bot ID already registered by another user.")

        path = self.config_dir / f"{config.bot_id}.json"
        write_json(path, config.dict())
        self._configs[config.bot_id] = config
        self._sessions[config.bot_id] = BotSession(config=config)
        self._ensure_bot_profile(config)
        logger.info("Registered bot %s (%s)", config.bot_id, config.provider)

    def delete_config(self, bot_id: str) -> None:
        with self._config_lock:
            self._delete_inner(bot_id)

    def _delete_inner(self, bot_id: str) -> None:
        path = self.config_dir / f"{bot_id}.json"
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                logger.error("Unable to remove bot config %s: %s", path, exc)
        self._configs.pop(bot_id, None)
        self._sessions.pop(bot_id, None)
        logger.info("Removed bot %s", bot_id)

    # ---------- runtime ----------
    async def on_chat_message(self, payload: Dict[str, Any]) -> None:
        """
        Consider responding to a new chat payload.
        """
        if not self._configs:
            return
        room = payload["room"]
        user_id = payload.get("user_id")
        text = payload.get("text", "")
        async with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            cfg = session.config
            bot_uid = self.bot_user_id(cfg.bot_id)
            if room not in cfg.rooms:
                continue
            if user_id == bot_uid:
                continue  # ignore our own echo
            if cfg.owner_id and not await self.hub.is_user_in_room(room, cfg.owner_id):
                continue  # owner not present
            if user_id and isinstance(user_id, str) and user_id.startswith("bot-") and not self._is_mention(cfg, text):
                continue  # avoid bot-to-bot chatter unless directly mentioned
            if not cfg.auto_reply and cfg.owner_id != user_id:
                # bot listens silently unless explicitly pinged
                if not self._is_mention(cfg, text):
                    continue
            if cfg.mention_only and not self._is_mention(cfg, text):
                continue
            if session.busy:
                continue
            await self._schedule_reply(session, payload)

    async def virtual_presence(self, room: str, present_user_ids: Set[str]) -> List[Dict[str, Any]]:
        """
        Provide bot profiles that should appear as co-present in a room (shadow presence).
        """
        result: List[Dict[str, Any]] = []
        if not self._configs:
            return result
        for cfg in self._configs.values():
            if not cfg.shadow_presence:
                continue
            if room not in cfg.rooms:
                continue
            if cfg.owner_id and cfg.owner_id not in present_user_ids:
                continue
            profile = self._ensure_bot_profile(cfg)
            if profile["user_id"] in present_user_ids:
                continue
            result.append(profile)
        return result

    async def nudge(self, room_id: str, hint: str) -> bool:
        """Ask the least-recently-used eligible bot in a room to speak."""
        async with self._lock:
            sessions = [s for s in self._sessions.values() if room_id in s.config.rooms and not s.busy]
        if not sessions:
            return False
        target = min(sessions, key=lambda s: s.last_reply_at or 0.0)
        synthetic_payload = {
            "room": room_id,
            "user_id": "__purfluous__",
            "user": "Sir Purfluous",
            "text": hint.strip(),
            "nudge": True,
            "ts": time.time(),
        }
        await self._schedule_reply(target, synthetic_payload)
        return True

    # ---------- helpers ----------
    def bot_user_id(self, bot_id: str) -> str:
        return f"bot-{bot_id}"

    def _ensure_bot_profile(self, config: BotConfig) -> Dict[str, Any]:
        uid = self.bot_user_id(config.bot_id)
        user_file = self.data_dir / "users" / f"{uid}.json"
        existing = read_json(user_file)
        base = UserState(
            user_id=uid,
            display_name=config.name,
            avatar_color=self._default_color(config.provider),
            badge=config.provider.value.capitalize(),
        )
        merged = {**base.dict(), **existing}
        try:
            state = UserState(**merged)
        except ValidationError as exc:
            logger.warning("Bot profile for %s invalid (%s). Resetting.", uid, exc)
            state = base
        write_json(user_file, state.dict())
        if hasattr(self.hub, "invalidate_user_cache"):
            self.hub.invalidate_user_cache(uid)
        return state.dict()

    def _default_color(self, provider: BotProvider) -> str:
        if provider is BotProvider.OPENAI:
            return "#7b68ee"
        if provider is BotProvider.GEMINI:
            return "#3ddad7"
        return "#888888"

    def _is_mention(self, config: BotConfig, text: str) -> bool:
        lowered = (text or "").lower()
        name = config.name.lower()
        return f"@{name}" in lowered or lowered.strip().startswith(name)

    async def _schedule_reply(self, session: BotSession, payload: Dict[str, Any]) -> None:
        session.busy = True
        task = asyncio.create_task(self._generate_and_send(session, payload))
        # Store reference to prevent GC cancellation; task clears itself on completion.
        session._pending_task = task
        task.add_done_callback(lambda t: setattr(session, '_pending_task', None))

    async def _generate_and_send(self, session: BotSession, payload: Dict[str, Any]) -> None:
        cfg = session.config
        try:
            history = await self.hub.get_recent_history(payload["room"], limit=cfg.max_history)
            if self._cricket_keeper is not None:
                try:
                    cricket = await self._cricket_keeper.get(cfg.bot_id)
                    incoming = (payload.get("text") or "").strip()
                    if incoming and payload.get("user_id") != self.bot_user_id(cfg.bot_id):
                        await cricket.remember(
                            content=f"{payload.get('user', payload.get('user_id', 'Someone'))}: {incoming}",
                            source="conversation",
                            importance=4,
                        )
                    history = await cricket.enrich_context(payload.get("text", ""), history)
                except Exception as memory_exc:
                    logger.warning("Jeremy Cricket memory failed for %s: %s", cfg.bot_id, memory_exc)
            prompt = self._build_prompt(cfg, history, stage_hint=payload.get("text") if payload.get("nudge") else None)
            if not prompt:
                return
            reply = await self._call_provider(cfg, prompt)
            if not reply:
                return
            reply = reply.strip()
            if not reply:
                return
            await self.hub.post_chat_message(payload["room"], self.bot_user_id(cfg.bot_id), reply)
            session.last_reply_at = time.time()
            if self._cricket_keeper is not None:
                try:
                    cricket = await self._cricket_keeper.get(cfg.bot_id)
                    await cricket.remember(content=reply, source="system", importance=5)
                except Exception as memory_exc:
                    logger.warning("Jeremy Cricket store failed for %s: %s", cfg.bot_id, memory_exc)
        except Exception as exc:
            logger.exception("Bot %s failed to reply: %s", cfg.bot_id, exc)
        finally:
            session.busy = False

    def _build_prompt(self, config: BotConfig, history: Sequence[Dict[str, Any]], stage_hint: Optional[str] = None) -> str:
        lines: List[str] = []
        if config.system_prompt:
            lines.append(f"System instruction: {config.system_prompt.strip()}")
        if stage_hint:
            lines.append(f"Stage direction (private): {stage_hint.strip()}")
        for entry in history:
            if entry.get("role") == "system":
                text = entry.get("text", "")
                if text:
                    lines.append(text)
                continue
            user = entry.get("user") or entry.get("sender_id") or entry.get("participant_id") or "User"
            text = entry.get("text", "")
            lines.append(f"{user}: {text}")
        lines.append(f"{config.name}:")
        return "\n".join(lines)

    async def _call_provider(self, config: BotConfig, prompt: str) -> str:
        if config.provider is BotProvider.OPENAI:
            return await self._call_openai(config, prompt)
        if config.provider is BotProvider.GEMINI:
            return await self._call_gemini(config, prompt)
        if config.provider is BotProvider.OLLAMA:
            return await self._call_ollama(config, prompt)
        raise RuntimeError(f"Unsupported provider {config.provider}")

    async def _call_openai(self, config: BotConfig, prompt: str) -> str:
        api_key = self._get_api_key(config)
        try:
            from openai import AsyncOpenAI  # type: ignore
        except ImportError as exc:
            raise RuntimeError("openai package is not installed. Run `pip install openai`.") from exc
        client = AsyncOpenAI(api_key=api_key)
        response = await client.responses.create(
            model=config.model,
            input=[{"role": "user", "content": prompt}],
            temperature=config.temperature,
        )
        parts = []
        if hasattr(response, "output"):
            for item in response.output:
                if getattr(item, "type", "") == "output_text":
                    parts.append(getattr(item, "text", ""))
        if not parts and hasattr(response, "output_text"):
            parts.append(getattr(response, "output_text"))
        return "".join(parts)

    async def _call_gemini(self, config: BotConfig, prompt: str) -> str:
        api_key = self._get_api_key(config)
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError as exc:
            raise RuntimeError("google-generativeai package is not installed. Run `pip install google-generativeai`.") from exc
        genai.configure(api_key=api_key)

        def _generate() -> str:
            model = genai.GenerativeModel(config.model, generation_config={"temperature": config.temperature})
            result = model.generate_content(prompt)
            if hasattr(result, "text") and result.text:
                return result.text
            candidates = getattr(result, "candidates", None)
            if candidates:
                for candidate in candidates:
                    content = getattr(candidate, "content", None)
                    if content and getattr(content, "parts", None):
                        texts = [getattr(part, "text", "") for part in content.parts]
                        joined = "".join(texts).strip()
                        if joined:
                            return joined
            return ""

        return await asyncio.to_thread(_generate)

    async def _call_ollama(self, config: BotConfig, prompt: str) -> str:
        model = resolve_ollama_model(config.model)
        reply = await call_ollama(
            prompt=prompt,
            model=model,
            temperature=config.temperature,
            max_tokens=512,
            timeout=settings.ollama_timeout,
            stream=settings.ollama_stream,
        )
        if not reply:
            logger.warning("Ollama call returned no text for %s using model %s", config.bot_id, model)
        return reply

    def _get_api_key(self, config: BotConfig) -> str:
        """
        Key resolution order:
          1. BYOK store — user-registered key for this provider (most specific)
          2. Environment variable (host-level key, works for all users)
          3. Raise RuntimeError with a clear message pointing to BYOK

        The BYOK lookup uses a synthetic user_id "host" for host-level keys so
        operators can register a default key without needing a real user session.
        """
        # 1. BYOK store lookup
        if self._byok_mgr is not None:
            provider = config.provider.value if hasattr(config.provider, 'value') else str(config.provider)
            try:
                import asyncio as _aio
                # get_adapter resolves the best key for this provider
                session_key = f"host:{provider}"
                stored = self._byok_mgr._store.get_model("host", provider)
                if stored:
                    key = self._byok_mgr._store.get_secret("host", provider, "api_key") or ""
                    if key:
                        logger.debug("Bot %s using BYOK key for provider=%s", config.bot_id, provider)
                        return key
                # Also check by model_id matching provider name
                for m in self._byok_mgr._store.list_models("host"):
                    if m.provider == provider:
                        key = self._byok_mgr._store.get_secret("host", m.model_id, "api_key") or ""
                        if key:
                            logger.debug("Bot %s using BYOK key model=%s", config.bot_id, m.model_id)
                            return key
            except Exception as exc:
                logger.debug("BYOK lookup failed for bot %s: %s", config.bot_id, exc)

        # 2. Environment variable
        key = os.getenv(config.api_key_env, "").strip()
        if key:
            return key

        # 3. Nothing found
        raise RuntimeError(
            f"No API key found for {config.name} (provider={config.provider}). "
            f"Either set env var '{config.api_key_env}' or register a key at /byok."
        )
