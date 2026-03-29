"""Optional split-LLM routing above the existing Studio/Ollama path."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from .appconfig import settings
from .ollama_provider import call_ollama, check_ollama_health, resolve_ollama_host, resolve_ollama_model, unload_ollama_model

logger = logging.getLogger("pubcast.split_llm")

RouteName = Literal["auto", "studio", "architect", "architect_then_studio"]

STUDIO_SYSTEM_PROMPT = (
    "You are PubCast Studio, the warm, readable, presentation-facing voice of PubCast. "
    "Favor clarity, supportive phrasing, natural flow, and emotionally legible language suitable for live chat and avatars."
)

ARCHITECT_SYSTEM_PROMPT = (
    "You are PubCast Architect, the internal technical analysis engine of PubCast. "
    "Be exact, concise, objective, and non-performative. Focus on findings, risks, fixes, validation, and system truth."
)

STUDIO_REPHRASE_PROMPT = (
    "You are PubCast Studio. Rewrite the Architect answer into a human-facing PubCast response without losing correctness."
)


@dataclass(frozen=True)
class RoleConfig:
    role: str
    provider: str
    model: str
    gguf_path: str
    system_prompt: str
    default_temperature: float
    default_max_tokens: int


class TaskRouter:
    _STUDIO_TYPES = {
        "avatar",
        "avatar_chat",
        "chat",
        "conversation",
        "host",
        "presentation",
        "studio",
        "user_facing",
        "user_facing_chat",
    }
    _ARCHITECT_TYPES = {
        "analysis",
        "architect",
        "code_review",
        "debug",
        "diagnostic",
        "diagnostics",
        "file_review",
        "plan",
        "planning",
        "repo_analysis",
        "safety",
        "systems_review",
        "technical",
    }
    _TWO_PASS_TYPES = {
        "architect_then_studio",
        "briefing",
        "diagnostic_rephrase",
        "report",
        "review_for_humans",
    }
    _ARCHITECT_KEYWORDS = (
        "bug",
        "code",
        "debug",
        "diagnostic",
        "endpoint",
        "error",
        "failing",
        "file ",
        "fix",
        "plan",
        "regression",
        "review",
        "route",
        "safety",
        "stack trace",
        "test",
    )

    def choose(
        self,
        *,
        requested_route: str = "auto",
        task_type: str = "",
        prompt: str = "",
        user_facing: Optional[bool] = None,
    ) -> RouteName:
        route = (requested_route or "auto").strip().lower()
        if route in {"studio", "architect", "architect_then_studio"}:
            return route  # type: ignore[return-value]

        task = (task_type or "").strip().lower()
        if task in self._STUDIO_TYPES:
            return "studio"
        if task in self._ARCHITECT_TYPES:
            return "architect"
        if task in self._TWO_PASS_TYPES:
            return "architect_then_studio"
        if user_facing is True:
            return "studio"
        lowered = (prompt or "").lower()
        if any(keyword in lowered for keyword in self._ARCHITECT_KEYWORDS):
            return "architect"
        return "studio"


class StudioBackend:
    def __init__(self, config: RoleConfig) -> None:
        self.config = config
        self._last_health: Dict[str, Any] = {}

    async def startup(self) -> Dict[str, Any]:
        self._last_health = await check_ollama_health(required_model=self.config.model)
        return self.status()

    def status(self) -> Dict[str, Any]:
        return {
            "provider": "ollama",
            "model": self.config.model,
            "gguf_path": self.config.gguf_path,
            "gguf_present": Path(self.config.gguf_path).exists(),
            "host": resolve_ollama_host(),
            "available": bool(self._last_health.get("alive") and self._last_health.get("model_available")),
            "alive": bool(self._last_health.get("alive")),
            "model_available": bool(self._last_health.get("model_available")),
            "available_models": self._last_health.get("models", []),
            "error": self._last_health.get("error"),
        }

    async def generate(
        self,
        *,
        prompt: str,
        system_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        started = time.time()
        text = await call_ollama(
            prompt=self._compose_prompt(prompt, system_prompt, "Studio"),
            model=self.config.model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=settings.ollama_timeout,
            stream=False,
            keep_alive=settings.ollama_keep_alive,
        )
        latency_ms = round((time.time() - started) * 1000.0, 1)
        if text:
            return {
                "text": text.strip(),
                "provider": "ollama",
                "model": self.config.model,
                "latency_ms": latency_ms,
                "error": None,
            }
        self._last_health = await check_ollama_health(required_model=self.config.model)
        return {
            "text": "",
            "provider": "ollama",
            "model": self.config.model,
            "latency_ms": latency_ms,
            "error": self._last_health.get("error") or "Studio returned no text",
        }

    async def unload(self) -> None:
        if settings.unload_on_switch:
            await unload_ollama_model(self.config.model)

    @staticmethod
    def _compose_prompt(prompt: str, system_prompt: str, speaker: str) -> str:
        return (
            f"System instruction: {system_prompt.strip()}\n\n"
            f"User request:\n{prompt.strip()}\n\n"
            f"{speaker}:"
        )


class ArchitectProvider:
    def __init__(self, config: RoleConfig) -> None:
        self.config = config
        self._runner_path = self._detect_runner()
        self._ollama_health: Dict[str, Any] = {}
        self._last_backend: str = "unavailable"

    def _detect_runner(self) -> str:
        configured = (settings.architect_runner_command or "").strip()
        if not configured:
            return ""
        if Path(configured).exists():
            return configured
        return shutil.which(configured) or ""

    async def startup(self) -> Dict[str, Any]:
        if self._provider_allows_ollama():
            self._ollama_health = await check_ollama_health(required_model=self.config.model)
        return self.status()

    def status(self) -> Dict[str, Any]:
        gguf = Path(self.config.gguf_path)
        runner_ready = bool(self._runner_path)
        gguf_ready = gguf.exists()
        backend = self._select_backend()
        error = None
        if backend == "ollama":
            if not self._ollama_health.get("alive"):
                error = self._ollama_health.get("error") or "Ollama not reachable"
            elif not self._ollama_health.get("model_available"):
                error = self._ollama_health.get("error") or f"Model '{self.config.model}' not available in Ollama"
        elif backend == "external_cli":
            if not runner_ready:
                error = f"Architect runner '{settings.architect_runner_command}' not found"
            elif not gguf_ready:
                error = f"Architect GGUF not found at {self.config.gguf_path}"
        else:
            if self._provider_allows_ollama():
                if not self._ollama_health:
                    error = "Architect backend unavailable (ollama not probed)"
                else:
                    error = self._ollama_health.get("error") or "Ollama not reachable"
            if not error and not runner_ready:
                error = f"Architect runner '{settings.architect_runner_command}' not found"
            if not error and not gguf_ready:
                error = f"Architect GGUF not found at {self.config.gguf_path}"
        return {
            "provider": settings.architect_provider,
            "backend": backend,
            "model": self.config.model,
            "gguf_path": self.config.gguf_path,
            "gguf_present": gguf_ready,
            "runner_command": settings.architect_runner_command,
            "runner_found": runner_ready,
            "ollama_host": resolve_ollama_host(),
            "ollama_alive": bool(self._ollama_health.get("alive")),
            "ollama_model_available": bool(self._ollama_health.get("model_available")),
            "ollama_error": self._ollama_health.get("error"),
            "available": backend in {"ollama", "external_cli"},
            "error": error,
        }

    def _provider_allows_ollama(self) -> bool:
        provider = (settings.architect_provider or "").strip().lower()
        return provider in {"ollama", "auto"}

    def _select_backend(self) -> str:
        provider = (settings.architect_provider or "").strip().lower()
        runner_ready = bool(self._runner_path) and Path(self.config.gguf_path).exists()
        ollama_ready = bool(self._ollama_health.get("alive") and self._ollama_health.get("model_available"))
        if provider in {"ollama", "auto"} and ollama_ready:
            self._last_backend = "ollama"
            return "ollama"
        if provider in {"external", "cli", "gguf"} and runner_ready:
            self._last_backend = "external_cli"
            return "external_cli"
        if provider == "auto" and runner_ready:
            self._last_backend = "external_cli"
            return "external_cli"
        self._last_backend = "unavailable"
        return "unavailable"

    async def generate(
        self,
        *,
        prompt: str,
        system_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        status = self.status()
        if not status["available"]:
            return {
                "text": "",
                "provider": settings.architect_provider,
                "model": self.config.model,
                "latency_ms": 0.0,
                "error": status["error"],
            }

        backend = status["backend"]
        started = time.time()
        if backend == "ollama":
            text = await call_ollama(
                prompt=prompt.strip(),
                model=self.config.model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=settings.ollama_timeout,
                stream=False,
                keep_alive=settings.ollama_keep_alive,
                system=system_prompt.strip(),
            )
            latency_ms = round((time.time() - started) * 1000.0, 1)
            if text:
                return {
                    "text": text.strip(),
                    "provider": "ollama",
                    "model": self.config.model,
                    "latency_ms": latency_ms,
                    "backend": "ollama",
                    "error": None,
                }
            if self._provider_allows_ollama():
                self._ollama_health = await check_ollama_health(required_model=self.config.model)
            if self._runner_path and Path(self.config.gguf_path).exists():
                fallback = await self._generate_cli(prompt, system_prompt, temperature, max_tokens)
                fallback["fallback_from"] = "ollama"
                return fallback
            return {
                "text": "",
                "provider": "ollama",
                "model": self.config.model,
                "latency_ms": latency_ms,
                "backend": "ollama",
                "error": self._ollama_health.get("error") or "Architect returned no text via Ollama",
            }

        return await self._generate_cli(prompt, system_prompt, temperature, max_tokens)

    async def _generate_cli(
        self,
        prompt: str,
        system_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        started = time.time()
        full_prompt = (
            f"{system_prompt.strip()}\n\n"
            f"User request:\n{prompt.strip()}\n\n"
            "Architect:"
        )
        cmd = [
            self._runner_path,
            "-m",
            self.config.gguf_path,
            "-p",
            full_prompt,
            "-n",
            str(max_tokens),
            "--temp",
            str(temperature),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=settings.architect_runner_timeout)
        except asyncio.TimeoutError:
            return {
                "text": "",
                "provider": settings.architect_provider,
                "model": self.config.model,
                "latency_ms": round((time.time() - started) * 1000.0, 1),
                "backend": "external_cli",
                "error": f"Architect runner timed out after {settings.architect_runner_timeout:.0f}s",
            }
        except Exception as exc:
            return {
                "text": "",
                "provider": settings.architect_provider,
                "model": self.config.model,
                "latency_ms": round((time.time() - started) * 1000.0, 1),
                "backend": "external_cli",
                "error": f"Architect runner failed to start: {exc}",
            }

        latency_ms = round((time.time() - started) * 1000.0, 1)
        output = stdout.decode("utf-8", errors="ignore").strip()
        err = stderr.decode("utf-8", errors="ignore").strip()
        if proc.returncode != 0:
            return {
                "text": "",
                "provider": settings.architect_provider,
                "model": self.config.model,
                "latency_ms": latency_ms,
                "backend": "external_cli",
                "error": err or f"Architect runner exited with code {proc.returncode}",
            }
        return {
            "text": output.strip(),
            "provider": settings.architect_provider,
            "model": self.config.model,
            "latency_ms": latency_ms,
            "backend": "external_cli",
            "error": None,
        }


class SplitLLMManager:
    def __init__(self) -> None:
        self._router = TaskRouter()
        self._studio = StudioBackend(
            RoleConfig(
                role="studio",
                provider=settings.studio_provider,
                model=resolve_ollama_model(settings.studio_model),
                gguf_path=settings.studio_gguf_path,
                system_prompt=STUDIO_SYSTEM_PROMPT,
                default_temperature=0.75,
                default_max_tokens=220,
            )
        )
        self._architect = ArchitectProvider(
            RoleConfig(
                role="architect",
                provider=settings.architect_provider,
                model=settings.architect_model,
                gguf_path=settings.architect_gguf_path,
                system_prompt=ARCHITECT_SYSTEM_PROMPT,
                default_temperature=0.2,
                default_max_tokens=320,
            )
        )
        self._active_role: Optional[str] = None
        self._active_provider: str = ""
        self._last_route: str = settings.default_role
        self._last_error: Optional[str] = None

    async def startup(self) -> Dict[str, Any]:
        await self._studio.startup()
        await self._architect.startup()
        architect_status = self._architect.status()
        if architect_status["available"]:
            if architect_status.get("backend") == "ollama":
                logger.info(
                    "Architect ready via Ollama model %s",
                    architect_status.get("model"),
                )
            else:
                logger.info(
                    "Architect runner detected: %s with %s",
                    architect_status.get("runner_command"),
                    architect_status.get("gguf_path"),
                )
        else:
            logger.info("Architect provider unavailable at startup: %s", architect_status["error"])
        return self.status()

    def status(self) -> Dict[str, Any]:
        return {
            "default_role": settings.default_role,
            "active_role": self._active_role,
            "active_provider": self._active_provider,
            "last_route": self._last_route,
            "last_error": self._last_error,
            "studio": self._studio.status(),
            "architect": self._architect.status(),
        }

    async def generate(
        self,
        *,
        prompt: str,
        requested_route: str = "auto",
        task_type: str = "",
        user_facing: Optional[bool] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        allow_fallback: Optional[bool] = None,
    ) -> Dict[str, Any]:
        route = self._router.choose(
            requested_route=requested_route,
            task_type=task_type,
            prompt=prompt,
            user_facing=user_facing,
        )
        self._last_route = route
        logger.info("Split router chose %s for task_type=%s", route, task_type or "unspecified")
        if route == "architect_then_studio" and not settings.allow_architect_then_studio:
            route = "studio"

        fallback_ok = settings.fallback_to_studio if allow_fallback is None else allow_fallback

        if route == "studio":
            return await self._generate_studio(prompt, temperature, max_tokens)
        if route == "architect":
            result = await self._generate_architect(prompt, temperature, max_tokens)
            if result["error"] and fallback_ok:
                result["fallback"] = "studio"
                studio = await self._generate_studio(prompt, temperature, max_tokens)
                studio["fallback_from"] = "architect"
                return studio
            return result

        architect = await self._generate_architect(prompt, temperature, max_tokens)
        if architect["error"]:
            if fallback_ok:
                studio = await self._generate_studio(prompt, temperature, max_tokens)
                studio["fallback_from"] = "architect_then_studio"
                return studio
            architect["path"] = "architect_then_studio"
            return architect

        studio_prompt = (
            f"Original request:\n{prompt.strip()}\n\n"
            f"Architect answer:\n{architect['text'].strip()}\n\n"
            "Respond for a human-facing PubCast context."
        )
        studio = await self._generate_studio(
            studio_prompt,
            temperature,
            max_tokens,
            system_prompt=STUDIO_REPHRASE_PROMPT,
        )
        studio["path"] = "architect_then_studio"
        studio["architect_text"] = architect["text"]
        studio["steps"] = [
            {"role": "architect", "provider": architect["provider"], "latency_ms": architect["latency_ms"]},
            {"role": "studio", "provider": studio["provider"], "latency_ms": studio["latency_ms"]},
        ]
        return studio

    async def _generate_studio(
        self,
        prompt: str,
        temperature: Optional[float],
        max_tokens: Optional[int],
        *,
        system_prompt: str = STUDIO_SYSTEM_PROMPT,
    ) -> Dict[str, Any]:
        if settings.unload_on_switch and self._active_role == "architect":
            logger.info("Switching Architect -> Studio")
        self._active_role = "studio"
        self._active_provider = "ollama"
        result = await self._studio.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature if temperature is not None else self._studio.config.default_temperature,
            max_tokens=max_tokens if max_tokens is not None else self._studio.config.default_max_tokens,
        )
        result["role"] = "studio"
        result["path"] = "studio"
        self._last_error = result["error"]
        return result

    async def _generate_architect(
        self,
        prompt: str,
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> Dict[str, Any]:
        if settings.unload_on_switch and self._active_role == "studio":
            logger.info("Switching Studio -> Architect")
            await self._studio.unload()
        self._active_role = "architect"
        self._active_provider = settings.architect_provider
        result = await self._architect.generate(
            prompt=prompt,
            system_prompt=ARCHITECT_SYSTEM_PROMPT,
            temperature=temperature if temperature is not None else self._architect.config.default_temperature,
            max_tokens=max_tokens if max_tokens is not None else self._architect.config.default_max_tokens,
        )
        result["role"] = "architect"
        result["path"] = "architect"
        self._last_error = result["error"]
        return result


__all__ = [
    "SplitLLMManager",
    "TaskRouter",
    "STUDIO_SYSTEM_PROMPT",
    "ARCHITECT_SYSTEM_PROMPT",
    "STUDIO_REPHRASE_PROMPT",
]
