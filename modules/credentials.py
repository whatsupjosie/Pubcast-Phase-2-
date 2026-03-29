from __future__ import annotations

import base64
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Dict, Iterable, List, Mapping, Optional

try:
    from cryptography.fernet import Fernet, InvalidToken
    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - fallback for lightweight envs
    _CRYPTO_AVAILABLE = False

    class InvalidToken(Exception):
        """Raised when decrypt fails in fallback cipher."""

    class _FallbackFernet:
        """Minimal base64-based cipher used when cryptography is unavailable."""

        def __init__(self, key: bytes):
            self.key = key

        def encrypt(self, data: bytes) -> str:
            return base64.urlsafe_b64encode(data).decode("utf-8")

        def decrypt(self, token, ttl=None) -> bytes:
            try:
                token_bytes = token.encode("utf-8") if isinstance(token, str) else token
                return base64.urlsafe_b64decode(token_bytes)
            except Exception as exc:  # noqa: BLE001
                raise InvalidToken from exc

    class Fernet:  # type: ignore[override]
        def __init__(self, key: bytes):
            self._impl = _FallbackFernet(key)

        def encrypt(self, data: bytes) -> str:
            return self._impl.encrypt(data)

        def decrypt(self, token: str, ttl=None) -> bytes:
            return self._impl.decrypt(token, ttl)

        @staticmethod
        def generate_key() -> bytes:
            return base64.urlsafe_b64encode(secrets.token_bytes(32))

from .persistence import sanitize_filename

DEFAULT_MASTER_KEY_ENV = "PUBCAST_MASTER_KEY"
MASTER_KEY_FILENAME = "master.key"
CREDS_DIR_NAME = "creds"
MODELS_DIR_NAME = "models"


class CredentialError(RuntimeError):
    """Raised for credential storage or retrieval failures."""


@dataclass
class SecretReference:
    """Reference metadata for a stored secret field."""

    name: str
    created_at: float
    last_updated: float


@dataclass
class StoredModel:
    """Represents non-secret metadata for a user model."""

    model_id: str
    provider: str
    kind: str = "hosted_api"
    display_name: str = ""
    model_name: str = ""            # alias for display_name used by main.py
    settings: Dict[str, object] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: __import__('time').time())
    updated_at: float = field(default_factory=lambda: __import__('time').time())
    last_verified: Optional[float] = None
    last_error: Optional[str] = None
    status: str = "pending"
    secret_fields: List[SecretReference] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "kind": self.kind,
            "display_name": self.display_name,
            "model_name": self.model_name,          # BUG-6 FIX: persist model_name
            "settings": self.settings,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_verified": self.last_verified,
            "last_error": self.last_error,
            "status": self.status,
            "secret_fields": [ref.__dict__ for ref in (self.secret_fields or [])],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "StoredModel":
        secret_fields = [
            SecretReference(
                name=item.get("name", ""),
                created_at=float(item.get("created_at", 0.0)),
                last_updated=float(item.get("last_updated", 0.0)),
            )
            for item in payload.get("secret_fields", [])
        ]
        return cls(
            model_id=str(payload["model_id"]),
            provider=str(payload["provider"]),
            kind=str(payload["kind"]),
            display_name=str(payload["display_name"]),
            model_name=str(payload.get("model_name", "")),  # BUG-6 FIX: restore model_name
            settings=dict(payload.get("settings", {})),
            created_at=float(payload.get("created_at", time.time())),
            updated_at=float(payload.get("updated_at", time.time())),
            last_verified=payload.get("last_verified"),
            last_error=payload.get("last_error"),
            status=str(payload.get("status", "pending")),
            secret_fields=secret_fields,
        )


class CredentialStore:
    """
    Stores encrypted credentials and associated model metadata per user.

    A master key is either provided via the PUBCAST_MASTER_KEY environment variable,
    or generated on first use and persisted to data/secure/master.key.
    """

    def __init__(self, base_dir: Path, *, master_key_env: str = DEFAULT_MASTER_KEY_ENV) -> None:
        self.base_dir = base_dir
        self.secure_dir = (base_dir / "secure").resolve()
        self.secure_dir.mkdir(parents=True, exist_ok=True)
        self.master_key_path = self.secure_dir / MASTER_KEY_FILENAME
        self.creds_dir = self.secure_dir / CREDS_DIR_NAME
        self.creds_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir = self.secure_dir / MODELS_DIR_NAME
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._fernet = Fernet(self._load_master_key(master_key_env))

    # ------------------------------------------------------------------ #
    # Public API

    def list_models(self, user_id: str) -> List[StoredModel]:
        payload = self._read_models_payload(user_id)
        models_payload = payload.get("models", [])
        return [StoredModel.from_dict(entry) for entry in models_payload]

    def get_model(self, user_id: str, model_id: str) -> Optional[StoredModel]:
        models = self.list_models(user_id)
        for model in models:
            if model.model_id == model_id:
                return model
        return None

    def upsert_model(
        self,
        user_id: str,
        model: StoredModel,
        secrets: Optional[Mapping[str, str]] = None,
    ) -> StoredModel:
        with self._lock:
            current = self.list_models(user_id)
            filtered = [m for m in current if m.model_id != model.model_id]
            existing = next((m for m in current if m.model_id == model.model_id), None)
            ref_index = {ref.name: ref for ref in (existing.secret_fields if existing else []) or []}
            if secrets is not None:
                now = time.time()
                for name, value in secrets.items():
                    is_remove = value == ""
                    self._store_secret(user_id, model.model_id, name, value)
                    if is_remove:
                        ref_index.pop(name, None)
                        continue
                    reference = ref_index.get(name)
                    if reference:
                        reference.last_updated = now
                    else:
                        ref_index[name] = SecretReference(name=name, created_at=now, last_updated=now)
            model.secret_fields = list(ref_index.values())
            payload = {"models": [m.to_dict() for m in filtered + [model]]}
            self._write_models_payload(user_id, payload)
        return model

    def patch_model_status(
        self,
        user_id: str,
        model_id: str,
        *,
        status: Optional[str] = None,
        last_verified: Optional[float] = None,
        last_error: Optional[str] = None,
    ) -> Optional[StoredModel]:
        with self._lock:
            models = self.list_models(user_id)
            updated: List[StoredModel] = []
            target: Optional[StoredModel] = None
            for model in models:
                if model.model_id == model_id:
                    if status:
                        model.status = status
                    if last_verified is not None:
                        model.last_verified = last_verified
                    if last_error is not None:
                        model.last_error = last_error
                    model.updated_at = time.time()
                    target = model
                updated.append(model)
            if target is None:
                return None
            payload = {"models": [m.to_dict() for m in updated]}
            self._write_models_payload(user_id, payload)
            return target

    def delete_model(self, user_id: str, model_id: str) -> None:
        with self._lock:
            models = self.list_models(user_id)
            filtered = [m for m in models if m.model_id != model_id]
            payload = {"models": [m.to_dict() for m in filtered]}
            self._write_models_payload(user_id, payload)
            self._remove_secrets(user_id, model_id)

    def has_secret(self, user_id: str, model_id: str, name: str) -> bool:
        payload = self._read_creds_payload(user_id)
        return name in payload.get(model_id, {})

    def get_secret(self, user_id: str, model_id: str, name: str) -> Optional[str]:
        payload = self._read_creds_payload(user_id)
        model_payload = payload.get(model_id) or {}
        token = model_payload.get(name)
        if not token:
            return None
        if isinstance(token, str):
            token_bytes = token.encode("utf-8")
        else:
            token_bytes = token
        try:
            decrypted = self._fernet.decrypt(token_bytes, ttl=None)
        except InvalidToken as exc:
            raise CredentialError(f"Unable to decrypt secret '{name}' for model '{model_id}'.") from exc
        return decrypted.decode("utf-8")

    def list_secret_names(self, user_id: str, model_id: str) -> Iterable[str]:
        payload = self._read_creds_payload(user_id)
        return (payload.get(model_id) or {}).keys()

    # ------------------------------------------------------------------ #
    # Internal helpers

    def _load_master_key(self, env_name: str) -> bytes:
        env_value = os.getenv(env_name)
        if env_value:
            try:
                decoded = base64.urlsafe_b64decode(env_value.encode("utf-8"))
            except (ValueError, TypeError) as exc:
                raise CredentialError(
                    f"Environment variable '{env_name}' must be a base64-urlsafe encoded key."
                ) from exc
            if len(decoded) != 32:
                raise CredentialError(f"Environment variable '{env_name}' must decode to 32 bytes.")
            return base64.urlsafe_b64encode(decoded)

        if self.master_key_path.exists():
            raw = self.master_key_path.read_bytes()
            return raw.strip()

        key = Fernet.generate_key()
        self.master_key_path.write_bytes(key)
        try:
            os.chmod(self.master_key_path, 0o600)
        except OSError:
            # Windows may not support chmod; ignore failures.
            pass
        return key

    def _store_secret(self, user_id: str, model_id: str, name: str, secret: str) -> None:
        payload = self._read_creds_payload(user_id)
        model_payload = payload.setdefault(model_id, {})
        if not secret:
            model_payload.pop(name, None)
        else:
            encrypted = self._fernet.encrypt(secret.encode("utf-8"))
            if isinstance(encrypted, bytes):
                encrypted = encrypted.decode("utf-8")
            model_payload[name] = encrypted
        self._write_creds_payload(user_id, payload)

    def _remove_secrets(self, user_id: str, model_id: str) -> None:
        payload = self._read_creds_payload(user_id)
        if model_id in payload:
            payload.pop(model_id)
            self._write_creds_payload(user_id, payload)

    def _read_creds_payload(self, user_id: str) -> Dict[str, Dict[str, str]]:
        path = self._creds_path(user_id)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_creds_payload(self, user_id: str, payload: Dict[str, Dict[str, str]]) -> None:
        path = self._creds_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _read_models_payload(self, user_id: str) -> Dict[str, object]:
        path = self._models_path(user_id)
        if not path.exists():
            return {"models": []}
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[arg-type]

    def _write_models_payload(self, user_id: str, payload: Dict[str, object]) -> None:
        path = self._models_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _creds_path(self, user_id: str) -> Path:
        safe = sanitize_filename(user_id)
        return self.creds_dir / f"{safe}.json"

    def _models_path(self, user_id: str) -> Path:
        safe = sanitize_filename(user_id)
        return self.models_dir / f"{safe}.json"


def generate_model_id() -> str:
    suffix = base64.urlsafe_b64encode(secrets.token_bytes(9)).decode("utf-8").rstrip("=")
    return f"mdl_{suffix.lower()}"
