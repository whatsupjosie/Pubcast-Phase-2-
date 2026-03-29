"""
modules/appconfig.py — Application configuration via environment variables.

Usage:
    from modules.appconfig import settings
    url = f"http://{settings.larynx_host}:{settings.larynx_port}/api/tts"
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # ── TTS (Larynx / Piper) ────────────────────────────────────────────────
    larynx_host: str = field(default_factory=lambda: os.environ.get("PUBCAST_LARYNX_HOST", "127.0.0.1"))
    larynx_port: int = field(default_factory=lambda: int(os.environ.get("PUBCAST_LARYNX_PORT", "5002")))
    piper_model: str = field(default_factory=lambda: os.environ.get("PUBCAST_PIPER_MODEL", "en_US-lessac-medium"))

    # ── Server ───────────────────────────────────────────────────────────────
    host: str = field(default_factory=lambda: os.environ.get("PUBCAST_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("PUBCAST_PORT", "8000")))
    debug: bool = field(default_factory=lambda: os.environ.get("PUBCAST_DEBUG", "0") == "1")

    # ── Storage paths ────────────────────────────────────────────────────────
    data_dir: str = field(default_factory=lambda: os.environ.get("PUBCAST_DATA_DIR", "data"))
    assets_dir: str = field(default_factory=lambda: os.environ.get("PUBCAST_ASSETS_DIR", "assets"))
    static_dir: str = field(default_factory=lambda: os.environ.get("PUBCAST_STATIC_DIR", "static"))

    # ── AI providers ─────────────────────────────────────────────────────────
    openai_key: str = field(default_factory=lambda: os.environ.get("PUBCAST_OPENAI_KEY", ""))
    anthropic_key: str = field(default_factory=lambda: os.environ.get("PUBCAST_ANTHROPIC_KEY", ""))
    google_key: str = field(default_factory=lambda: os.environ.get("PUBCAST_GOOGLE_KEY", ""))
    llm_provider: str = field(default_factory=lambda: os.environ.get("LLM_PROVIDER", "ollama"))
    studio_provider: str = field(default_factory=lambda: os.environ.get("PUBCAST_STUDIO_PROVIDER", "ollama"))
    ollama_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "OLLAMA_BASE_URL",
            os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        )
    )
    studio_model: str = field(
        default_factory=lambda: os.environ.get(
            "PUBCAST_STUDIO_MODEL",
            os.environ.get("OLLAMA_MODEL", "google_gemma-3-1b-it-Q4_K_L"),
        )
    )
    studio_gguf_path: str = field(
        default_factory=lambda: os.environ.get("PUBCAST_STUDIO_GGUF", "google_gemma-3-1b-it-Q4_K_L.gguf")
    )
    architect_model: str = field(
        default_factory=lambda: os.environ.get("PUBCAST_ARCHITECT_MODEL", "google_gemma-3-1b-it-Q6_K")
    )
    architect_provider: str = field(
        default_factory=lambda: os.environ.get("PUBCAST_ARCHITECT_PROVIDER", "auto")
    )
    architect_gguf_path: str = field(
        default_factory=lambda: os.environ.get("PUBCAST_ARCHITECT_GGUF", "google_gemma-3-1b-it-Q6_K.gguf")
    )
    architect_runner_command: str = field(
        default_factory=lambda: os.environ.get("PUBCAST_ARCHITECT_RUNNER_COMMAND", "llama-cli")
    )
    architect_runner_timeout: float = field(
        default_factory=lambda: float(os.environ.get("PUBCAST_ARCHITECT_RUNNER_TIMEOUT", "180"))
    )
    ollama_model: str = field(
        default_factory=lambda: os.environ.get(
            "OLLAMA_MODEL",
            os.environ.get("PUBCAST_STUDIO_MODEL", "google_gemma-3-1b-it-Q4_K_L"),
        )
    )
    default_role: str = field(default_factory=lambda: os.environ.get("PUBCAST_DEFAULT_ROLE", "studio"))
    allow_architect_then_studio: bool = field(
        default_factory=lambda: os.environ.get("PUBCAST_ALLOW_ARCHITECT_THEN_STUDIO", "1") == "1"
    )
    architect_required: bool = field(
        default_factory=lambda: os.environ.get("PUBCAST_ARCHITECT_REQUIRED", "0") == "1"
    )
    unload_on_switch: bool = field(default_factory=lambda: os.environ.get("PUBCAST_UNLOAD_ON_SWITCH", "1") == "1")
    fallback_to_studio: bool = field(default_factory=lambda: os.environ.get("PUBCAST_FALLBACK_TO_STUDIO", "1") == "1")
    ollama_timeout: float = field(default_factory=lambda: float(os.environ.get("OLLAMA_TIMEOUT", "60")))
    ollama_stream: bool = field(default_factory=lambda: os.environ.get("OLLAMA_STREAM", "0") == "1")
    ollama_keep_alive: str = field(default_factory=lambda: os.environ.get("OLLAMA_KEEP_ALIVE", "60s"))
    ollama_health_timeout: float = field(
        default_factory=lambda: float(os.environ.get("OLLAMA_HEALTH_TIMEOUT", "5"))
    )

    # ── Recording ────────────────────────────────────────────────────────────
    max_recording_hours: float = field(
        default_factory=lambda: float(os.environ.get("PUBCAST_MAX_RECORDING_HOURS", "4"))
    )


# Single shared instance — imported by name throughout the app
settings = Settings()
