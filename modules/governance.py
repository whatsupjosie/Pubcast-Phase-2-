"""
modules/governance.py — PubCast Governance Engine
══════════════════════════════════════════════════
Moderation, safety, consent, and audit for PubCast AI productions.

CAPABILITIES:
  - User bans (temporary + permanent) with reason tracking
  - Avatar freeze (host takes control of a participant's avatar)
  - Mute / force-mute with duration
  - Consent management (recording consent, AI disclosure, terms acceptance)
  - AI disclosure — every AI participant announces itself on entry
  - Black box audit logger — tamper-evident log of all governance actions
  - Waiting room / airlock — users must accept terms before entering
  - Session entry control — host can approve/deny waiting room requests

CONSENT MODEL:
  Before entering any room, a user must:
    1. Accept the Terms of Service
    2. Acknowledge AI Disclosure (AI participants are present)
    3. Consent to recording policy for the target room
    4. Wait for host approval (if room requires it)

  AI bots must:
    1. Be registered with a disclosure statement
    2. Announce themselves when entering a room
    3. Have their owner's consent on file

Rear View Foresight LLC — Feic Mo Chroí — 2026-03-24T09:00Z
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("pubcast.governance")


# ─── Enums ─────────────────────────────────────────────────────────────────────

class BanType(str, Enum):
    TEMPORARY = "temporary"
    PERMANENT = "permanent"
    SESSION = "session"


class ConsentType(str, Enum):
    TERMS_OF_SERVICE = "tos"
    RECORDING = "recording"
    AI_DISCLOSURE = "ai_disclosure"
    DATA_PROCESSING = "data_processing"


class ModerationAction(str, Enum):
    BAN = "ban"
    UNBAN = "unban"
    MUTE = "mute"
    UNMUTE = "unmute"
    FREEZE = "freeze"
    UNFREEZE = "unfreeze"
    KICK = "kick"
    WARN = "warn"
    APPROVE_ENTRY = "approve_entry"
    DENY_ENTRY = "deny_entry"


class EntryStatus(str, Enum):
    WAITING = "waiting"
    APPROVED = "approved"
    DENIED = "denied"
    ENTERED = "entered"
    EXPIRED = "expired"


# ─── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class BanRecord:
    ban_id: str
    user_id: str
    ban_type: BanType
    reason: str
    issued_by: str
    issued_at: float
    expires_at: Optional[float] = None
    lifted_at: Optional[float] = None
    lifted_by: Optional[str] = None

    @property
    def is_active(self) -> bool:
        if self.lifted_at is not None:
            return False
        if self.ban_type == BanType.TEMPORARY and self.expires_at:
            return time.time() < self.expires_at
        return True


@dataclass
class FreezeState:
    """When an avatar is frozen, the host controls their movement."""
    user_id: str
    frozen_by: str
    frozen_at: float
    reason: str = ""
    pose_override: Optional[Dict[str, Any]] = None


@dataclass
class ConsentRecord:
    user_id: str
    consent_type: ConsentType
    granted: bool
    granted_at: float
    ip_hash: str = ""
    version: str = "1.0"


@dataclass
class WaitingRoomEntry:
    entry_id: str
    user_id: str
    display_name: str
    target_room: str
    requested_at: float
    status: EntryStatus = EntryStatus.WAITING
    resolved_by: Optional[str] = None
    resolved_at: Optional[float] = None
    consents: Dict[str, bool] = field(default_factory=dict)


# ─── AI Disclosure ─────────────────────────────────────────────────────────────

AI_DISCLOSURE_TEXT = """
═══ AI PARTICIPANT DISCLOSURE ═══

This PubCast AI production includes artificial intelligence participants.
AI characters in this session may include:

{character_list}

These AI participants:
  • Generate responses using large language models (LLMs)
  • May remember context from this conversation
  • Are not human beings and do not have consciousness
  • Are governed by PubCast's content policies
  • May be recording and processing your interactions

By entering this room, you acknowledge the presence of AI participants
and consent to interacting with them.

Your interactions may be used to improve the AI experience.
You may request data deletion at any time.

═══ Rear View Foresight LLC — Feic Mo Chroí ═══
"""

TERMS_OF_SERVICE = """
═══ PUBCAST AI — TERMS OF SERVICE ═══

1. RESPECT: Treat all participants (human and AI) with respect.
2. CONTENT: You are responsible for what you say and create.
3. RECORDING: Some rooms are recorded. Privacy indicators are visible.
4. AI PRESENCE: AI characters are present and disclosed.
5. MODERATION: Hosts may mute, freeze, or remove participants.
6. DATA: Your messages are stored for the session. Contact us to delete.
7. OWNERSHIP: You own your creative contributions. PubCast owns the platform.

By clicking "I Accept," you agree to these terms.

═══ Rear View Foresight LLC — Feic Mo Chroí ═══
"""


# ─── Governance Engine ─────────────────────────────────────────────────────────

class GovernanceEngine:
    """
    Central governance authority for a PubCast production.

    Manages bans, freezes, mutes, consent, and the waiting room.
    All actions are logged to a tamper-evident black box audit trail.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir / "governance"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._data_dir / "governance.db"

        # In-memory state (fast lookups)
        self._bans: Dict[str, BanRecord] = {}
        self._freezes: Dict[str, FreezeState] = {}
        self._mutes: Dict[str, float] = {}  # user_id → mute_until (0 = indefinite)
        self._consents: Dict[str, Dict[str, ConsentRecord]] = {}
        self._waiting_room: Dict[str, WaitingRoomEntry] = {}

        self._init_db()
        self._load_active_bans()

    def _init_db(self):
        conn = sqlite3.connect(self._db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bans (
                ban_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                ban_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                issued_by TEXT NOT NULL,
                issued_at REAL NOT NULL,
                expires_at REAL,
                lifted_at REAL,
                lifted_by TEXT
            );
            CREATE TABLE IF NOT EXISTS consents (
                user_id TEXT NOT NULL,
                consent_type TEXT NOT NULL,
                granted INTEGER NOT NULL,
                granted_at REAL NOT NULL,
                ip_hash TEXT DEFAULT '',
                version TEXT DEFAULT '1.0',
                PRIMARY KEY (user_id, consent_type)
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                action TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                target_id TEXT,
                details TEXT,
                prev_hash TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_bans_user ON bans(user_id);
            CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp);
        """)
        conn.commit()
        conn.close()

    def _load_active_bans(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM bans WHERE lifted_at IS NULL"
        ).fetchall()
        conn.close()
        for row in rows:
            ban = BanRecord(
                ban_id=row["ban_id"],
                user_id=row["user_id"],
                ban_type=BanType(row["ban_type"]),
                reason=row["reason"],
                issued_by=row["issued_by"],
                issued_at=row["issued_at"],
                expires_at=row["expires_at"],
            )
            if ban.is_active:
                self._bans[ban.user_id] = ban

    # ═══ BANNING ═══════════════════════════════════════════════════════════════

    def ban_user(
        self,
        user_id: str,
        reason: str,
        issued_by: str,
        ban_type: BanType = BanType.SESSION,
        duration_hours: Optional[float] = None,
    ) -> BanRecord:
        """Ban a user. Session bans expire when the server restarts."""
        ban_id = str(uuid.uuid4())[:12]
        expires_at = None
        if ban_type == BanType.TEMPORARY and duration_hours:
            expires_at = time.time() + (duration_hours * 3600)

        ban = BanRecord(
            ban_id=ban_id,
            user_id=user_id,
            ban_type=ban_type,
            reason=reason,
            issued_by=issued_by,
            issued_at=time.time(),
            expires_at=expires_at,
        )
        self._bans[user_id] = ban

        if ban_type != BanType.SESSION:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "INSERT INTO bans VALUES (?,?,?,?,?,?,?,?,?)",
                (ban.ban_id, ban.user_id, ban.ban_type.value, ban.reason,
                 ban.issued_by, ban.issued_at, ban.expires_at, None, None),
            )
            conn.commit()
            conn.close()

        self._audit("BAN", issued_by, user_id,
                     f"type={ban_type.value}, reason={reason}, "
                     f"duration={'permanent' if not duration_hours else f'{duration_hours}h'}")
        logger.info("Banned %s by %s: %s", user_id, issued_by, reason)
        return ban

    def unban_user(self, user_id: str, lifted_by: str) -> bool:
        """Lift a ban."""
        ban = self._bans.pop(user_id, None)
        if not ban:
            return False
        ban.lifted_at = time.time()
        ban.lifted_by = lifted_by

        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "UPDATE bans SET lifted_at=?, lifted_by=? WHERE ban_id=?",
            (ban.lifted_at, lifted_by, ban.ban_id),
        )
        conn.commit()
        conn.close()

        self._audit("UNBAN", lifted_by, user_id, f"ban_id={ban.ban_id}")
        return True

    def is_banned(self, user_id: str) -> Tuple[bool, Optional[str]]:
        """Check if a user is banned. Returns (banned, reason)."""
        ban = self._bans.get(user_id)
        if ban and ban.is_active:
            return True, ban.reason
        if ban and not ban.is_active:
            self._bans.pop(user_id, None)
        return False, None

    # ═══ AVATAR FREEZE ═════════════════════════════════════════════════════════

    def freeze_avatar(
        self,
        user_id: str,
        frozen_by: str,
        reason: str = "",
        pose_override: Optional[Dict[str, Any]] = None,
    ) -> FreezeState:
        """
        Freeze a participant's avatar. The host takes control of their movement.
        The user can still see and hear but cannot move or gesture.
        """
        state = FreezeState(
            user_id=user_id,
            frozen_by=frozen_by,
            frozen_at=time.time(),
            reason=reason,
            pose_override=pose_override,
        )
        self._freezes[user_id] = state
        self._audit("FREEZE", frozen_by, user_id, f"reason={reason}")
        logger.info("Avatar frozen: %s by %s", user_id, frozen_by)
        return state

    def unfreeze_avatar(self, user_id: str, unfrozen_by: str) -> bool:
        """Release avatar control back to the user."""
        if user_id not in self._freezes:
            return False
        del self._freezes[user_id]
        self._audit("UNFREEZE", unfrozen_by, user_id, "")
        return True

    def is_frozen(self, user_id: str) -> Tuple[bool, Optional[FreezeState]]:
        state = self._freezes.get(user_id)
        return (True, state) if state else (False, None)

    def get_freeze_pose(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get the pose override for a frozen avatar (if any)."""
        state = self._freezes.get(user_id)
        return state.pose_override if state else None

    # ═══ MUTING ════════════════════════════════════════════════════════════════

    def mute_user(
        self,
        user_id: str,
        muted_by: str,
        duration_seconds: float = 0,
    ) -> None:
        """Mute a user. 0 = indefinite."""
        until = time.time() + duration_seconds if duration_seconds > 0 else 0
        self._mutes[user_id] = until
        self._audit("MUTE", muted_by, user_id,
                     f"duration={'indefinite' if not duration_seconds else f'{duration_seconds}s'}")

    def unmute_user(self, user_id: str, unmuted_by: str) -> bool:
        if user_id in self._mutes:
            del self._mutes[user_id]
            self._audit("UNMUTE", unmuted_by, user_id, "")
            return True
        return False

    def is_muted(self, user_id: str) -> bool:
        until = self._mutes.get(user_id)
        if until is None:
            return False
        if until == 0:
            return True  # indefinite
        if time.time() < until:
            return True
        del self._mutes[user_id]
        return False

    # ═══ CONSENT ═══════════════════════════════════════════════════════════════

    def record_consent(
        self,
        user_id: str,
        consent_type: ConsentType,
        granted: bool,
        ip_hash: str = "",
    ) -> ConsentRecord:
        """Record a user's consent decision."""
        record = ConsentRecord(
            user_id=user_id,
            consent_type=consent_type,
            granted=granted,
            granted_at=time.time(),
            ip_hash=ip_hash,
        )
        self._consents.setdefault(user_id, {})[consent_type.value] = record

        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT OR REPLACE INTO consents VALUES (?,?,?,?,?,?)",
            (user_id, consent_type.value, int(granted), record.granted_at,
             ip_hash, record.version),
        )
        conn.commit()
        conn.close()

        self._audit("CONSENT", user_id, None,
                     f"type={consent_type.value}, granted={granted}")
        return record

    def has_consent(self, user_id: str, consent_type: ConsentType) -> bool:
        """Check if a user has granted a specific consent."""
        user_consents = self._consents.get(user_id, {})
        record = user_consents.get(consent_type.value)
        if record and record.granted:
            return True
        # Check DB fallback
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT granted FROM consents WHERE user_id=? AND consent_type=?",
            (user_id, consent_type.value),
        ).fetchone()
        conn.close()
        return bool(row and row[0])

    def get_missing_consents(self, user_id: str) -> List[ConsentType]:
        """Return which consents the user hasn't granted yet."""
        missing = []
        for ct in [ConsentType.TERMS_OF_SERVICE, ConsentType.AI_DISCLOSURE]:
            if not self.has_consent(user_id, ct):
                missing.append(ct)
        return missing

    def get_all_consents(self, user_id: str) -> Dict[str, bool]:
        """Return all consent statuses for a user."""
        result = {}
        for ct in ConsentType:
            result[ct.value] = self.has_consent(user_id, ct)
        return result

    # ═══ WAITING ROOM / AIRLOCK ════════════════════════════════════════════════

    def request_entry(
        self,
        user_id: str,
        display_name: str,
        target_room: str,
        consents: Dict[str, bool] = None,
    ) -> WaitingRoomEntry:
        """User requests to enter a room. Goes to waiting room first."""
        entry = WaitingRoomEntry(
            entry_id=str(uuid.uuid4())[:12],
            user_id=user_id,
            display_name=display_name,
            target_room=target_room,
            requested_at=time.time(),
            consents=consents or {},
        )
        self._waiting_room[entry.entry_id] = entry
        self._audit("ENTRY_REQUEST", user_id, None,
                     f"room={target_room}, name={display_name}")
        return entry

    def approve_entry(self, entry_id: str, approved_by: str) -> Optional[WaitingRoomEntry]:
        """Host approves a waiting room request."""
        entry = self._waiting_room.get(entry_id)
        if not entry or entry.status != EntryStatus.WAITING:
            return None
        entry.status = EntryStatus.APPROVED
        entry.resolved_by = approved_by
        entry.resolved_at = time.time()
        self._audit("APPROVE_ENTRY", approved_by, entry.user_id,
                     f"room={entry.target_room}")
        return entry

    def deny_entry(
        self,
        entry_id: str,
        denied_by: str,
        reason: str = "",
    ) -> Optional[WaitingRoomEntry]:
        """Host denies a waiting room request."""
        entry = self._waiting_room.get(entry_id)
        if not entry or entry.status != EntryStatus.WAITING:
            return None
        entry.status = EntryStatus.DENIED
        entry.resolved_by = denied_by
        entry.resolved_at = time.time()
        self._audit("DENY_ENTRY", denied_by, entry.user_id,
                     f"room={entry.target_room}, reason={reason}")
        return entry

    def mark_entered(self, entry_id: str) -> None:
        """Mark that the user has actually entered the room."""
        entry = self._waiting_room.get(entry_id)
        if entry:
            entry.status = EntryStatus.ENTERED

    def list_waiting(self, target_room: str = None) -> List[WaitingRoomEntry]:
        """List users in the waiting room."""
        entries = [e for e in self._waiting_room.values()
                   if e.status == EntryStatus.WAITING]
        if target_room:
            entries = [e for e in entries if e.target_room == target_room]
        return entries

    def can_enter(self, user_id: str) -> Tuple[bool, str]:
        """
        Full entry check. Returns (allowed, reason).
        Checks: not banned, has required consents.
        """
        banned, reason = self.is_banned(user_id)
        if banned:
            return False, f"You are banned: {reason}"

        missing = self.get_missing_consents(user_id)
        if missing:
            names = ", ".join(ct.value for ct in missing)
            return False, f"Missing required consents: {names}"

        return True, "OK"

    # ═══ AI DISCLOSURE ═════════════════════════════════════════════════════════

    def get_ai_disclosure(self, bot_names: List[str]) -> str:
        """Generate the AI disclosure text for a room."""
        char_list = "\n".join(f"  • {name}" for name in bot_names)
        return AI_DISCLOSURE_TEXT.format(character_list=char_list)

    def get_terms_of_service(self) -> str:
        return TERMS_OF_SERVICE

    # ═══ BLACK BOX AUDIT ═══════════════════════════════════════════════════════

    def _audit(self, action: str, actor_id: str, target_id: Optional[str],
               details: str) -> None:
        """Tamper-evident audit log entry with hash chain."""
        conn = sqlite3.connect(self._db_path)
        # Get previous hash
        prev = conn.execute(
            "SELECT log_id, timestamp, details FROM audit_log "
            "ORDER BY log_id DESC LIMIT 1"
        ).fetchone()
        prev_hash = ""
        if prev:
            chain_input = f"{prev[0]}:{prev[1]}:{prev[2]}"
            prev_hash = hashlib.sha256(chain_input.encode()).hexdigest()[:16]

        conn.execute(
            "INSERT INTO audit_log (timestamp, action, actor_id, target_id, "
            "details, prev_hash) VALUES (?,?,?,?,?,?)",
            (time.time(), action, actor_id, target_id, details, prev_hash),
        )
        conn.commit()
        conn.close()

    def get_audit_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_user_history(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all governance actions involving a specific user."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE actor_id=? OR target_id=? "
            "ORDER BY timestamp DESC LIMIT 200",
            (user_id, user_id),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ═══ QUERY ═════════════════════════════════════════════════════════════════

    def list_active_bans(self) -> List[BanRecord]:
        return [b for b in self._bans.values() if b.is_active]

    def list_frozen_avatars(self) -> List[FreezeState]:
        return list(self._freezes.values())

    def list_muted_users(self) -> List[str]:
        return [uid for uid in self._mutes if self.is_muted(uid)]
