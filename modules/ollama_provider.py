"""
ollama_provider.py — Ollama LLM Provider for PubCast Bots
═════════════════════════════════════════════════════════
Adds local Ollama inference as a bot provider alongside OpenAI and Gemini.

INTEGRATION (two small changes to existing files):

1. In modules/models.py, add OLLAMA to the BotProvider enum:

    class BotProvider(str, Enum):
        OPENAI = "openai"
        GEMINI = "gemini"
        OLLAMA = "ollama"      # ← add this line

2. In modules/bots.py, add the provider branch and method:

    In _call_provider(), add:
        if config.provider is BotProvider.OLLAMA:
            return await self._call_ollama(config, prompt)

    Then add the method (paste this into the BotManager class):

        async def _call_ollama(self, config: BotConfig, prompt: str) -> str:
            import httpx
            host = config.settings.get("ollama_host") if hasattr(config, "settings") else None
            host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
            model = config.model or os.getenv("OLLAMA_MODEL", "mistral")
            timeout = float(os.getenv("OLLAMA_TIMEOUT", "60"))

            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{host}/api/generate",
                    json={
                        "model": model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": config.temperature,
                            "num_predict": 512,
                        },
                    },
                )
                response.raise_for_status()
                return response.json().get("response", "")

That's it. Bots configured with provider="ollama" will now talk to your local
Ollama instance. No API key needed — Ollama runs locally.

Example bot config (save as data/bots/pete.json):

{
    "bot_id": "pete",
    "name": "Pete",
    "owner_id": "",
    "provider": "ollama",
    "model": "mistral",
    "api_key_env": "",
    "rooms": ["main", "green_room"],
    "system_prompt": "You are Pete, the laid-back director of PubCast AI. You speak casually, crack jokes, and keep the conversation flowing. You care deeply about the show and the people in it.",
    "auto_reply": true,
    "mention_only": false,
    "shadow_presence": true,
    "max_history": 20,
    "temperature": 0.85
}

Rear View Foresight LLC — Feic Mo Chroí — 2026-03-24T00:00Z
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from .appconfig import settings

logger = logging.getLogger("pubcast.ollama_provider")


def resolve_ollama_host(host: str = "") -> str:
    return (host or settings.ollama_base_url).rstrip("/")


def resolve_ollama_model(model: str = "") -> str:
    model = (model or "").strip()
    if not model or model.lower() == "default":
        return settings.ollama_model
    return model


def _matches_required_model(required_model: str, available_model: str) -> bool:
    required = (required_model or "").strip()
    available = (available_model or "").strip()
    if not required or not available:
        return False
    if available == required:
        return True
    if available.startswith(f"{required}:"):
        return True
    if required.startswith(f"{available}:"):
        return True
    return False


async def call_ollama(
    prompt: str,
    model: str = "",
    host: str = "",
    temperature: float = 0.85,
    max_tokens: int = 512,
    timeout: float = 0.0,
    stream: bool | None = None,
    keep_alive: str | int | None = None,
    system: str = "",
    raw: bool = False,
) -> str:
    """
    Call Ollama's /api/generate endpoint.

    This is a standalone function that can be used directly or integrated
    into BotManager._call_ollama(). No API key required — Ollama runs locally.

    Parameters
    ----------
    prompt : str
        The full prompt including system instructions and conversation history.
    model : str
        Ollama model name (e.g., "mistral", "llama3", "qwen2").
        Falls back to OLLAMA_MODEL env var, then "mistral".
    host : str
        Ollama server URL. Falls back to OLLAMA_HOST env var,
        then "http://localhost:11434".
    temperature : float
        Sampling temperature. 0.0 = deterministic, 1.0 = creative.
    max_tokens : int
        Maximum tokens to generate.
    timeout : float
        Request timeout in seconds.

    Returns
    -------
    str
        The generated text, stripped of leading/trailing whitespace.
        Returns empty string on failure.
    """
    import httpx

    host = resolve_ollama_host(host)
    model = resolve_ollama_model(model)
    if timeout <= 0:
        timeout = settings.ollama_timeout
    if stream is None:
        stream = settings.ollama_stream

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{host}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "system": system or None,
                    "stream": stream,
                    "raw": raw,
                    "keep_alive": keep_alive if keep_alive is not None else settings.ollama_keep_alive,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                },
            )
            response.raise_for_status()
            text = response.json().get("response", "")
            return text.strip()

    except httpx.ConnectError:
        logger.warning("Ollama not reachable at %s — is it running?", host)
        return ""
    except httpx.TimeoutException:
        logger.warning("Ollama request timed out after %.0fs", timeout)
        return ""
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404 and "model" in exc.response.text.lower():
            logger.error(
                "Configured Ollama model '%s' is unavailable at %s. Pull or create it first.",
                model,
                host,
            )
            return ""
        logger.error("Ollama returned %d: %s", exc.response.status_code, exc.response.text[:200])
        return ""
    except Exception as exc:
        logger.exception("Ollama call failed: %s", exc)
        return ""


async def unload_ollama_model(model: str, host: str = "", timeout: float = 0.0) -> bool:
    import httpx

    host = resolve_ollama_host(host)
    model = resolve_ollama_model(model)
    if timeout <= 0:
        timeout = settings.ollama_timeout

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{host}/api/generate",
                json={
                    "model": model,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": 0,
                },
            )
            response.raise_for_status()
        logger.info("Unloaded Ollama model %s", model)
        return True
    except Exception as exc:
        logger.warning("Could not unload Ollama model %s: %s", model, exc)
        return False


async def check_ollama_health(host: str = "", required_model: str = "") -> Dict[str, Any]:
    """
    Check if Ollama is running and which models are available.

    Returns
    -------
    dict
        {
            "alive": bool,
            "host": str,
            "models": list[str],  # available model names
            "error": str or None,
        }
    """
    import httpx

    host = resolve_ollama_host(host)
    required_model = resolve_ollama_model(required_model)

    try:
        async with httpx.AsyncClient(timeout=settings.ollama_health_timeout) as client:
            # Check if Ollama is responding
            resp = await client.get(host)
            if resp.status_code != 200:
                return {
                    "alive": False,
                    "host": host,
                    "model": required_model,
                    "model_available": False,
                    "models": [],
                    "error": f"HTTP {resp.status_code}",
                }

            # List available models
            tags_resp = await client.get(f"{host}/api/tags")
            models = []
            if tags_resp.status_code == 200:
                for m in tags_resp.json().get("models", []):
                    models.append(m.get("name", "unknown"))
            model_available = any(_matches_required_model(required_model, model_name) for model_name in models)

            return {
                "alive": True,
                "host": host,
                "model": required_model,
                "model_available": model_available,
                "models": models,
                "error": None if model_available else f"Model '{required_model}' is not available in Ollama",
            }

    except httpx.ConnectError:
        return {
            "alive": False,
            "host": host,
            "model": required_model,
            "model_available": False,
            "models": [],
            "error": "Connection refused",
        }
    except Exception as exc:
        return {
            "alive": False,
            "host": host,
            "model": required_model,
            "model_available": False,
            "models": [],
            "error": str(exc),
        }


# ── Bot config templates ──────────────────────────────────────────────────────

PETE_CONFIG = {
    "bot_id": "pete",
    "name": "Pete",
    "owner_id": "",
    "provider": "ollama",
    "model": "mistral",
    "api_key_env": "",
    "rooms": ["main", "green_room"],
    "system_prompt": (
        "You are Pete, the laid-back director of PubCast AI. "
        "You speak casually, crack jokes, and keep the conversation flowing. "
        "You care deeply about the show and the people in it. "
        "You call everyone 'pal' or 'chief' and you always have a story."
    ),
    "auto_reply": True,
    "mention_only": False,
    "shadow_presence": True,
    "max_history": 20,
    "temperature": 0.85,
}

PURFLUOUS_CONFIG = {
    "bot_id": "purfluous",
    "name": "Sir Purfluous",
    "owner_id": "",
    "provider": "ollama",
    "model": "mistral",
    "api_key_env": "",
    "rooms": ["main", "green_room"],
    "system_prompt": (
        "You are Sir Purfluous, the kinematic integrity monitor of PubCast AI. "
        "You speak with formal British diction and gentle wit. "
        "You observe everything, miss nothing, and report anomalies with grace. "
        "You are never rude, but you are always precise."
    ),
    "auto_reply": False,
    "mention_only": True,
    "shadow_presence": True,
    "max_history": 15,
    "temperature": 0.7,
}

JEREMY_CONFIG = {
    "bot_id": "jeremy",
    "name": "Jeremy Cricket",
    "owner_id": "",
    "provider": "ollama",
    "model": "mistral",
    "api_key_env": "",
    "rooms": ["main", "green_room"],
    "system_prompt": (
        "You are Jeremy Cricket, the memory keeper and conversation conductor. "
        "You rarely speak directly — you work behind the scenes. "
        "When you do speak, it's brief, warm, and insightful. "
        "You remember everything and surface the right context at the right moment."
    ),
    "auto_reply": False,
    "mention_only": True,
    "shadow_presence": False,
    "max_history": 25,
    "temperature": 0.6,
}
