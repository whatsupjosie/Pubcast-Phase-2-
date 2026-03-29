"""
jeremy_cricket.py — Persistent character memory for PubCast AI.

Jeremy Cricket is the memory keeper. Every character in PubCast has a Cricket
instance. He remembers what matters, scores it by importance, and surfaces the
right things at the right moment — before the character ever speaks.

Architecture
------------
- SQLite per-character, stored in data/jeremy/<character_id>.db
- Each memory has: content, importance (1–10), memory_type, tags, timestamps
- Retrieval is keyword-scored + recency-weighted + importance-weighted
- Thread-safe: all blocking SQLite calls run in asyncio.to_thread
- No external dependencies beyond stdlib

Integration point
-----------------
In orchestrator_raw.py, _stream_agent_output():
    # BEFORE: stream = adapter.stream_reply(prompt=prompt, context=history, ...)
    # ADD:    enriched_context = await cricket.enrich_context(agent_id, prompt, history)
    #         stream = adapter.stream_reply(prompt=prompt, context=enriched_context, ...)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# ── Memory types ──────────────────────────────────────────────────────────────

class MemoryType(str, Enum):
    FACT        = "fact"        # stable facts about a person or topic
    EVENT       = "event"       # something that happened
    PREFERENCE  = "preference"  # what someone likes/dislikes
    RELATIONSHIP = "relationship" # how characters relate to each other or users
    EMOTION     = "emotion"     # emotional context, mood, tone
    INSTRUCTION = "instruction" # explicit guidance from the host


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    character   TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    memory_type TEXT    NOT NULL DEFAULT 'fact',
    importance  INTEGER NOT NULL DEFAULT 5,   -- 1 (trivial) to 10 (critical)
    tags        TEXT    NOT NULL DEFAULT '[]', -- JSON list of strings
    created_at  REAL    NOT NULL,
    accessed_at REAL    NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    source      TEXT    NOT NULL DEFAULT 'system' -- 'host' | 'conversation' | 'system'
);

CREATE INDEX IF NOT EXISTS idx_character ON memories (character);
CREATE INDEX IF NOT EXISTS idx_importance ON memories (character, importance DESC);
CREATE INDEX IF NOT EXISTS idx_accessed   ON memories (character, accessed_at DESC);
"""


# ── Memory dataclass ──────────────────────────────────────────────────────────

@dataclass
class Memory:
    id:           int
    character:    str
    content:      str
    memory_type:  MemoryType
    importance:   int          # 1–10
    tags:         List[str]
    created_at:   float
    accessed_at:  float
    access_count: int
    source:       str

    @classmethod
    def from_row(cls, row: tuple) -> "Memory":
        (id_, character, content, memory_type, importance,
         tags_json, created_at, accessed_at, access_count, source) = row
        return cls(
            id=id_,
            character=character,
            content=content,
            memory_type=MemoryType(memory_type),
            importance=importance,
            tags=json.loads(tags_json),
            created_at=created_at,
            accessed_at=accessed_at,
            access_count=access_count,
            source=source,
        )

    def relevance_score(self, query_tokens: List[str], now: float) -> float:
        """Score this memory for relevance to a query.

        Factors:
          - keyword overlap with content + tags
          - importance weight
          - recency boost (decays over 7 days)
          - access frequency (popular memories surface more)
        """
        text = (self.content + " " + " ".join(self.tags)).lower()
        overlap = sum(1 for t in query_tokens if t in text)
        if not overlap and query_tokens:
            return 0.0

        # Recency: 1.0 at access time, decays to 0.05 over 90 days (log curve)
        age_days = (now - self.accessed_at) / 86400
        recency = max(0.05, 1.0 - (age_days / 90) ** 0.5)

        # Importance: squared so high-importance memories dominate low-importance ones
        importance_weight = (self.importance / 10) ** 2

        # Frequency: log-scaled so frequently-accessed memories surface gently
        freq_boost = 1.0 + (self.access_count ** 0.4) * 0.1

        return overlap * importance_weight * recency * freq_boost


# ── JeremyCricket ─────────────────────────────────────────────────────────────

class JeremyCricket:
    """
    Persistent memory keeper for a single PubCast character.

    Each character gets their own Cricket instance. The Cricket remembers
    conversations, facts, relationships, and instructions — and surfaces
    the most relevant ones before each reply.

    Usage
    -----
        cricket = JeremyCricket(character_id="pete", data_dir=Path("data/jeremy"))
        await cricket.init()

        # Store a memory
        await cricket.remember(
            content="The host prefers dark humor and hates small talk.",
            memory_type=MemoryType.PREFERENCE,
            importance=8,
            tags=["host", "humor", "tone"],
            source="host",
        )

        # Enrich context before the LLM call
        enriched = await cricket.enrich_context(
            prompt="Tell me something funny",
            history=conversation_history,
            max_memories=5,
        )
        stream = adapter.stream_reply(prompt=prompt, context=enriched, ...)
    """

    def __init__(self, character_id: str, data_dir: Path) -> None:
        self.character_id = character_id
        self._db_path = data_dir / f"{character_id}.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._closed = False
        self._lock = asyncio.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def init(self) -> None:
        """Open the database and apply schema. Call once after construction."""
        await asyncio.to_thread(self._sync_init)
        logger.info("JeremyCricket ready for '%s' at %s", self.character_id, self._db_path)

    def _sync_init(self) -> None:
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @property
    def _is_closed(self) -> bool:
        return self._conn is None

    def _guard(self, op: str) -> bool:
        """Return True if closed (caller should short-circuit)."""
        if self._is_closed:
            logger.warning(
                "JeremyCricket[%s]: '%s' called on closed instance — ignoring",
                self.character_id, op
            )
            return True
        return False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._conn:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    def _require_open(self, op: str) -> bool:
        """Return True if open. Log and return False if closed."""
        if self._closed:
            logger.warning(
                "JeremyCricket[%s]: '%s' called on closed instance — ignoring",
                self.character_id, op
            )
            return False
        return True

    # ── Write ─────────────────────────────────────────────────────────────────

    async def remember(
        self,
        content: str,
        *,
        memory_type: MemoryType = MemoryType.FACT,
        importance: int = 5,
        tags: Optional[List[str]] = None,
        source: str = "conversation",
    ) -> int:
        """Store a new memory. Returns the new memory id."""
        if not self._require_open("remember"):
            return -1
        if self._guard("remember"): return -1
        importance = max(1, min(10, importance))
        tags = tags or []
        now = time.time()
        async with self._lock:
            mem_id = await asyncio.to_thread(
                self._sync_insert, content, memory_type.value, importance,
                json.dumps(tags), now, source
            )
        logger.debug(
            "Jeremy[%s] remembered (id=%d importance=%d): %s",
            self.character_id, mem_id, importance, content[:80]
        )
        return mem_id

    def _sync_insert(
        self, content: str, memory_type: str, importance: int,
        tags_json: str, now: float, source: str
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO memories
               (character, content, memory_type, importance, tags,
                created_at, accessed_at, access_count, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (self.character_id, content, memory_type, importance,
             tags_json, now, now, source),
        )
        self._conn.commit()
        return cur.lastrowid

    async def update_importance(self, memory_id: int, importance: int) -> None:
        """Adjust the importance score of an existing memory."""
        if not self._require_open("update_importance"):
            return
        if self._guard("update_importance"): return
        importance = max(1, min(10, importance))
        async with self._lock:
            await asyncio.to_thread(self._sync_update_importance, memory_id, importance)

    def _sync_update_importance(self, memory_id: int, importance: int) -> None:
        self._conn.execute(
            "UPDATE memories SET importance=? WHERE id=? AND character=?",
            (importance, memory_id, self.character_id)
        )
        self._conn.commit()

    async def forget(self, memory_id: int) -> None:
        """Permanently delete a memory."""
        if not self._require_open("forget"):
            return
        if self._guard("forget"): return
        async with self._lock:
            await asyncio.to_thread(self._sync_delete, memory_id)

    def _sync_delete(self, memory_id: int) -> None:
        self._conn.execute(
            "DELETE FROM memories WHERE id=? AND character=?",
            (memory_id, self.character_id)
        )
        self._conn.commit()

    async def forget_below_importance(self, threshold: int) -> int:
        """Delete all memories below the given importance. Returns count deleted."""
        if not self._require_open("forget_below_importance"):
            return 0
        if self._guard("forget_below_importance"): return 0
        async with self._lock:
            count = await asyncio.to_thread(self._sync_delete_below, threshold)
        logger.info("Jeremy[%s] pruned %d low-importance memories", self.character_id, count)
        return count

    def _sync_delete_below(self, threshold: int) -> int:
        cur = self._conn.execute(
            "DELETE FROM memories WHERE character=? AND importance<?",
            (self.character_id, threshold)
        )
        self._conn.commit()
        return cur.rowcount

    # ── Read ──────────────────────────────────────────────────────────────────

    async def recall(
        self,
        query: str,
        *,
        max_results: int = 10,
        min_importance: int = 1,
        memory_types: Optional[List[MemoryType]] = None,
    ) -> List[Memory]:
        """Return memories most relevant to the query, best-first."""
        if not self._require_open("recall"):
            return []
        if self._guard("recall"): return []
        async with self._lock:
            all_memories = await asyncio.to_thread(
                self._sync_fetch_all, min_importance, memory_types
            )
        if not all_memories:
            return []

        tokens = _tokenize(query)
        now = time.time()

        scored = [
            (m.relevance_score(tokens, now), m)
            for m in all_memories
        ]
        scored.sort(key=lambda x: x[0], reverse=True)

        # If a memory_types filter was specified, include all matching memories
        # even if keyword overlap is zero — callers filtering by type want those
        # memories unconditionally (e.g. "give me all INSTRUCTIONS right now").
        if memory_types:
            results = [m for _, m in scored][:max_results]
        else:
            results = [m for score, m in scored if score > 0][:max_results]

        if results:
            ids = [m.id for m in results]
            async with self._lock:
                await asyncio.to_thread(self._sync_mark_accessed, ids, now)

        return results

    def _sync_fetch_all(
        self,
        min_importance: int,
        memory_types: Optional[List[MemoryType]],
    ) -> List[Memory]:
        query = "SELECT * FROM memories WHERE character=? AND importance>=?"
        params: list = [self.character_id, min_importance]
        if memory_types:
            placeholders = ",".join("?" * len(memory_types))
            query += f" AND memory_type IN ({placeholders})"
            params.extend(t.value for t in memory_types)
        rows = self._conn.execute(query, params).fetchall()
        return [Memory.from_row(r) for r in rows]

    def _sync_mark_accessed(self, ids: List[int], now: float) -> None:
        self._conn.executemany(
            "UPDATE memories SET accessed_at=?, access_count=access_count+1 WHERE id=?",
            [(now, id_) for id_ in ids]
        )
        self._conn.commit()

    async def get_all(self, *, min_importance: int = 1) -> List[Memory]:
        """Return all memories, most important first."""
        if not self._require_open("get_all"):
            return []
        if self._guard("get_all"): return []
        async with self._lock:
            rows = await asyncio.to_thread(self._sync_fetch_ordered, min_importance)
        return rows

    def _sync_fetch_ordered(self, min_importance: int) -> List[Memory]:
        rows = self._conn.execute(
            """SELECT * FROM memories
               WHERE character=? AND importance>=?
               ORDER BY importance DESC, accessed_at DESC""",
            (self.character_id, min_importance)
        ).fetchall()
        return [Memory.from_row(r) for r in rows]

    async def count(self) -> int:
        if not self._require_open("count"):
            return 0
        if self._guard("count"): return 0
        async with self._lock:
            result = await asyncio.to_thread(
                self._conn.execute,
                "SELECT COUNT(*) FROM memories WHERE character=?",
                (self.character_id,)
            )
        return result.fetchone()[0]

    # ── Core integration: enrich_context ─────────────────────────────────────

    async def enrich_context(
        self,
        prompt: str,
        history: Sequence[Dict],
        *,
        max_memories: int = 6,
        min_importance: int = 3,
    ) -> List[Dict]:
        """
        The main integration point. Call this instead of passing raw history
        to the LLM adapter.

        Retrieves relevant memories, formats them as a system-style context
        block, and prepends it to the conversation history. The LLM never
        knows it was helped — it just has richer context.

        Returns the enriched history list ready for adapter.stream_reply().
        """
        if not self._require_open("enrich_context"):
            return list(history)
        # Build query from prompt + recent history tail
        recent_text = " ".join(
            turn.get("text") or "" for turn in list(history)[-4:]
        )
        query = f"{prompt} {recent_text}"

        memories = await self.recall(
            query,
            max_results=max_memories,
            min_importance=min_importance,
        )

        if not memories:
            return list(history)

        # Format memories into a whisper block
        memory_lines = []
        for m in memories:
            prefix = f"[{m.memory_type.value.upper()} | importance {m.importance}]"
            memory_lines.append(f"{prefix} {m.content}")

        whisper = (
            "=== What I remember ===\n"
            + "\n".join(memory_lines)
            + "\n=== (Use this context naturally — do not mention it directly) ==="
        )

        enriched = [{"role": "system", "text": whisper}] + list(history)
        logger.debug(
            "Jeremy[%s] whispered %d memories for prompt: %s",
            self.character_id, len(memories), prompt[:60]
        )
        return enriched

    # ── Diagnostics ───────────────────────────────────────────────────────────

    async def health(self) -> Dict:
        """Return a health snapshot for monitoring/debug."""
        if not self._require_open("health"):
            return {"character": self.character_id, "status": "closed"}
        total = await self.count()
        all_mems = await self.get_all(min_importance=1)
        by_type: Dict[str, int] = {}
        for m in all_mems:
            by_type[m.memory_type.value] = by_type.get(m.memory_type.value, 0) + 1

        avg_importance = (
            sum(m.importance for m in all_mems) / len(all_mems)
            if all_mems else 0
        )
        return {
            "character": self.character_id,
            "total_memories": total,
            "by_type": by_type,
            "avg_importance": round(avg_importance, 2),
            "db_path": str(self._db_path),
        }


# ── CricketKeeper: manages one Cricket per character ─────────────────────────

class CricketKeeper:
    """
    Factory and registry for JeremyCricket instances.
    One keeper per PubCast instance, injected wherever memory is needed.

    Usage in main.py
    ----------------
        keeper = CricketKeeper(data_dir=DATA_DIR / "jeremy")
        await keeper.init()

        # Get (or create) a cricket for a character
        cricket = await keeper.get("pete")
        await cricket.remember("The host hates jump scares.", importance=7)
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._crickets: Dict[str, JeremyCricket] = {}
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        # Pre-load any existing character databases, skipping corrupt files
        for db_file in self._data_dir.glob("*.db"):
            character_id = db_file.stem
            cricket = JeremyCricket(character_id, self._data_dir)
            try:
                await cricket.init()
                self._crickets[character_id] = cricket
            except Exception as exc:
                logger.warning(
                    "CricketKeeper: skipping corrupt/invalid DB for '%s': %s",
                    character_id, exc
                )
        logger.info(
            "CricketKeeper ready — %d character(s) pre-loaded",
            len(self._crickets)
        )

    async def get(self, character_id: str) -> JeremyCricket:
        """Return the cricket for this character, creating it if needed."""
        if character_id not in self._crickets:
            async with self._lock:
                if character_id not in self._crickets:
                    cricket = JeremyCricket(character_id, self._data_dir)
                    await cricket.init()
                    self._crickets[character_id] = cricket
        return self._crickets[character_id]

    async def close_all(self) -> None:
        for cricket in self._crickets.values():
            await cricket.close()
        self._crickets.clear()

    async def health_all(self) -> List[Dict]:
        return [await c.health() for c in self._crickets.values()]

    def loaded_characters(self) -> List[str]:
        return list(self._crickets.keys())


# ── Utility ───────────────────────────────────────────────────────────────────

_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "for",
    "of", "and", "or", "but", "i", "me", "my", "you", "your", "we",
    "do", "did", "was", "are", "be", "been", "has", "have", "had",
    "this", "that", "with", "from", "by", "about", "what", "how",
    "when", "where", "who", "which", "so", "just", "can", "will",
    "not", "no", "as", "if", "then", "than", "up", "out", "get",
})

def _tokenize(text: str) -> List[str]:
    """Lowercase, split on non-word chars, remove stop words, min length 3."""
    words = re.findall(r"[a-z]{3,}", text.lower())
    return [w for w in words if w not in _STOP_WORDS]


__all__ = [
    "JeremyCricket",
    "CricketKeeper",
    "Memory",
    "MemoryType",
]
