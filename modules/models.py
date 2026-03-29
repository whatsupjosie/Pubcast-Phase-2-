"""
modules/models.py — PubCast shared data models
================================================
Single source of truth for all Pydantic/dataclass models used across
the modules package. Both the main application layer (bots, hub, etc.)
and the BYOK credential sub-system import from here.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Production & User ─────────────────────────────────────────────────────────

class ProductionState(BaseModel):
    mode: str = Field(default="LIVE", description="LIVE or REPLAY")
    on_air: bool = False
    replay_asset: str = ""       # e.g. assets/vod/demo.mp4 (relative to root)
    lower_third: str = ""
    camera: str = "wide"         # 'wide' | 'closeup' | 'overhead'
    lighting: str = "normal"     # 'normal' | 'warm' | 'cool'


class UserState(BaseModel):
    user_id: str
    display_name: str = "User"
    avatar_color: str = "#00e0ff"
    badge: str = ""
    last_room: str = "dressing"


# ── Bot co-hosts ──────────────────────────────────────────────────────────────

class BotProvider(str, Enum):
    OPENAI = "openai"
    GEMINI = "gemini"
    OLLAMA = "ollama"


class BotConfig(BaseModel):
    """Persistent configuration for an AI co-host bot."""
    bot_id: str
    name: str
    owner_id: str = ""
    provider: BotProvider
    model: str
    api_key_env: str                          # name of the env var holding the key
    rooms: List[str] = Field(default_factory=list)
    system_prompt: str = ""
    auto_reply: bool = True
    mention_only: bool = False
    shadow_presence: bool = True              # show in roster alongside owner
    max_history: int = 20
    temperature: float = 0.85


# ── BYOK credential sub-system ────────────────────────────────────────────────

class CredentialStatus(str, Enum):
    PENDING  = "pending"
    READY    = "ready"
    ERROR    = "error"
    DISABLED = "disabled"


class ModelKind(str, Enum):
    HOSTED_API     = "hosted_api"       # OpenAI, Anthropic, Gemini …
    LOCAL_HTTP     = "local_http"       # Ollama, LM Studio, llama.cpp server
    LOCAL_COMMAND  = "local_command"    # subprocess-based inference


class SecretField(str, Enum):
    """Canonical names for secret fields stored encrypted in CredentialStore."""
    API_KEY         = "api_key"
    SECRET_KEY      = "secret_key"
    ACCESS_TOKEN    = "access_token"
    CLIENT_SECRET   = "client_secret"
    SERVICE_ACCOUNT = "service_account_json"


class UserModelConfig(BaseModel):
    """Non-secret metadata persisted alongside encrypted credentials."""
    model_id: str
    display_name: str
    provider: str
    kind: ModelKind
    status: CredentialStatus = CredentialStatus.PENDING
    last_verified: Optional[float] = None
    # Provider-specific settings (base_url, model name, etc.)
    settings: Dict[str, Any] = Field(default_factory=dict)
    # Which secret field names are stored for this model
    secret_field_names: List[str] = Field(default_factory=list)


class UserModelStatus(BaseModel):
    """Summary view returned by UserModelManager.credential_summary()."""
    provider: str
    model_count: int
    worst_status: CredentialStatus
    models: List[UserModelConfig] = Field(default_factory=list)
