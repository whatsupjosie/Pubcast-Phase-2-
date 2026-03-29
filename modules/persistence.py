"""
modules/persistence.py — PubCast I/O helpers
=============================================
Sync and async JSON persistence, filename sanitisation, upload validation.
All async functions use aiofiles; the dependency is declared in requirements.txt.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

# aiofiles is a runtime dependency.  Import lazily so unit tests that stub
# the filesystem can patch at module level without triggering the import.
try:
    import aiofiles
    _AIOFILES = True
except ImportError:  # pragma: no cover
    _AIOFILES = False

ALLOWED_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webm", ".mp4",
    ".pdf", ".txt", ".md", ".csv", ".ppt", ".pptx",
    ".wav", ".mov", ".zip",
}


# ── Directory setup ───────────────────────────────────────────────────────────

def ensure_dirs(data_dir: Path, assets_dir: Path) -> None:
    """Create standard PubCast data and asset directories."""
    for sub in ("users", "logs", "global", "avatars", "bots", "recordings",
                "exports", "imports", "projects", "security"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("vod", "uploads", "models", "textures"):
        (assets_dir / sub).mkdir(parents=True, exist_ok=True)


# ── Sync JSON helpers ─────────────────────────────────────────────────────────

def read_json(path: Path) -> Dict[str, Any]:
    """Read a JSON file; return {} if the file does not exist."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Dict[str, Any]) -> None:
    """Atomically write *data* to *path* as pretty-printed JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp sibling then rename for atomicity on POSIX.
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    """Append one JSON object as a line to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ── Async JSON helpers ────────────────────────────────────────────────────────

async def read_json_async(path: Path) -> Dict[str, Any]:
    """Async equivalent of read_json.  Falls back to sync if aiofiles is absent."""
    if not path.exists():
        return {}
    if _AIOFILES:
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            raw = await f.read()
        return json.loads(raw)
    # Fallback (should not happen in production but keeps tests simple)
    return read_json(path)


async def write_json_async(path: Path, data: Dict[str, Any]) -> None:
    """Async equivalent of write_json with the same atomic-rename strategy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        if _AIOFILES:
            async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
                await f.write(payload)
        else:
            tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ── Filename helpers ──────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    """Strip path traversal components and dangerous characters."""
    name = name.strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name or "_"


def allowed_upload_extension(name: str) -> bool:
    """Return True if the file extension is on the allowlist."""
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    return ext in ALLOWED_EXTS


def build_autosave_filename(
    prefix: str,
    suffix: str,
    *,
    when: float | None = None,
) -> str:
    """
    Build a timestamped autosave filename.

    Example::

        build_autosave_filename("autosave", ".json")
        # → "autosave_20260216T045201Z.json"

        build_autosave_filename("autosave", ".json", when=1765701240.0)
        # → "autosave_20251214T083400Z.json"
    """
    ts = when if when is not None else time.time()
    stamp = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}{suffix}"
