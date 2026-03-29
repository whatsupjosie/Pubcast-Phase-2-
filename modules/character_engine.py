"""
modules/character_engine.py — Character Intelligence Layer
═══════════════════════════════════════════════════════════
Everything that makes PubCast bots feel alive:

  1. Speaking Slot Enforcer — bots don't talk over each other
  2. Thought Logger — every LLM call (prompt + response) is recorded
  3. Prompt Templates — tuned system prompts for Pete, Purfluous, Jeremy
  4. Nudge Calibration — sigmoid parameters for Jeremy's timing
  5. Bot Test Harness — feed sample conversations, verify character voice

Rear View Foresight LLC — Feic Mo Chroí — 2026-03-24
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("pubcast.character_engine")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. SPEAKING SLOT ENFORCER
# ═══════════════════════════════════════════════════════════════════════════════

class SpeakingSlotEnforcer:
    """
    Prevents bots from talking over each other.

    Rules:
      - Minimum gap between any two bot messages: configurable (default 4s)
      - Only one bot may be generating at a time (global lock)
      - Priority queue: mentioned bots go first, then least-recently-spoken
      - Humans always interrupt — no queue for human messages
      - Mode-aware: LIVE mode has longer gaps than REHEARSAL

    Usage:
        enforcer = SpeakingSlotEnforcer()
        if await enforcer.acquire_slot("bot-pete"):
            try:
                response = await generate_response(...)
                await enforcer.record_spoke("bot-pete")
            finally:
                enforcer.release_slot("bot-pete")
    """

    def __init__(self, min_gap: float = 4.0, max_concurrent: int = 1):
        self._min_gap = min_gap
        self._max_concurrent = max_concurrent
        self._last_spoke: Dict[str, float] = {}
        self._active_speakers: set = set()
        self._lock = asyncio.Lock()
        self._queue: List[Tuple[float, str]] = []  # (priority, bot_id)

    async def acquire_slot(self, bot_id: str, priority: float = 0.0,
                           timeout: float = 10.0) -> bool:
        """
        Try to acquire a speaking slot. Returns True if the bot may speak.
        Blocks until slot is available or timeout expires.
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            async with self._lock:
                # Check concurrent limit
                if len(self._active_speakers) >= self._max_concurrent:
                    pass  # Wait
                else:
                    # Check gap since last bot spoke
                    last_any = max(self._last_spoke.values()) if self._last_spoke else 0
                    gap = time.time() - last_any
                    if gap >= self._min_gap:
                        self._active_speakers.add(bot_id)
                        return True

            await asyncio.sleep(0.5)

        logger.debug("Speaking slot timeout for %s", bot_id)
        return False

    def release_slot(self, bot_id: str) -> None:
        """Release a speaking slot after the bot finishes."""
        self._active_speakers.discard(bot_id)

    async def record_spoke(self, bot_id: str) -> None:
        """Record that a bot just spoke (updates timing)."""
        async with self._lock:
            self._last_spoke[bot_id] = time.time()

    def set_min_gap(self, seconds: float) -> None:
        """Adjust the minimum gap (e.g., from mode resolver)."""
        self._min_gap = max(1.0, seconds)

    def time_since_last_bot(self) -> float:
        """How long since any bot last spoke."""
        if not self._last_spoke:
            return float('inf')
        return time.time() - max(self._last_spoke.values())

    def least_recent_bot(self, candidates: List[str]) -> Optional[str]:
        """Return the bot from candidates that spoke least recently."""
        if not candidates:
            return None
        return min(candidates, key=lambda b: self._last_spoke.get(b, 0))

    def status(self) -> Dict[str, Any]:
        return {
            "min_gap": self._min_gap,
            "active_speakers": list(self._active_speakers),
            "last_spoke": {k: round(time.time() - v, 1) for k, v in self._last_spoke.items()},
            "time_since_last_bot": round(self.time_since_last_bot(), 1),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 2. THOUGHT LOGGER
# ═══════════════════════════════════════════════════════════════════════════════

class ThoughtLogger:
    """
    Logs every LLM call: the full prompt, the response, timing, token estimates,
    and the bot that made the call. Stored in SQLite for review.

    This is how you review "what the AIs thought" — every decision, every
    response, every nudge that Jeremy sent.
    """

    def __init__(self, data_dir: Path):
        self._db_path = data_dir / "thought_log.db"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thoughts (
                thought_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                bot_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                prompt_length INTEGER NOT NULL,
                response_length INTEGER NOT NULL,
                latency_ms REAL NOT NULL,
                prompt_text TEXT NOT NULL,
                response_text TEXT NOT NULL,
                room TEXT DEFAULT '',
                trigger_type TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thoughts_bot ON thoughts(bot_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thoughts_time ON thoughts(timestamp)")
        conn.commit()
        conn.close()

    def log_thought(
        self,
        bot_id: str,
        provider: str,
        model: str,
        prompt: str,
        response: str,
        latency_ms: float,
        room: str = "",
        trigger_type: str = "chat",
        metadata: Dict[str, Any] = None,
    ) -> int:
        """Log a thought. Returns the thought_id."""
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute(
            """INSERT INTO thoughts
               (timestamp, bot_id, provider, model, prompt_hash, prompt_length,
                response_length, latency_ms, prompt_text, response_text,
                room, trigger_type, metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                time.time(), bot_id, provider, model,
                hashlib.md5(prompt.encode()).hexdigest()[:12],
                len(prompt), len(response), latency_ms,
                prompt, response, room, trigger_type,
                json.dumps(metadata or {}),
            ),
        )
        thought_id = cursor.lastrowid
        conn.commit()
        conn.close()
        logger.debug("Thought logged: bot=%s, latency=%.0fms, response=%d chars",
                      bot_id, latency_ms, len(response))
        return thought_id

    def get_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT thought_id, timestamp, bot_id, provider, model, "
            "prompt_length, response_length, latency_ms, room, trigger_type, "
            "response_text FROM thoughts ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_thought(self, thought_id: int) -> Optional[Dict[str, Any]]:
        """Get full thought including prompt text."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM thoughts WHERE thought_id=?", (thought_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_bot_thoughts(self, bot_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM thoughts WHERE bot_id=? ORDER BY timestamp DESC LIMIT ?",
            (bot_id, limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def stats(self) -> Dict[str, Any]:
        conn = sqlite3.connect(self._db_path)
        total = conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()[0]
        by_bot = conn.execute(
            "SELECT bot_id, COUNT(*), AVG(latency_ms), AVG(response_length) "
            "FROM thoughts GROUP BY bot_id"
        ).fetchall()
        conn.close()
        return {
            "total_thoughts": total,
            "by_bot": {
                row[0]: {
                    "count": row[1],
                    "avg_latency_ms": round(row[2] or 0, 1),
                    "avg_response_length": round(row[3] or 0, 0),
                }
                for row in by_bot
            },
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 3. PROMPT TEMPLATES — Tuned for Ollama/Mistral
# ═══════════════════════════════════════════════════════════════════════════════

PROMPT_TEMPLATES = {
    "pete": {
        "system": (
            "You are Pete, the director of PubCast AI — a virtual production studio. "
            "You have pink hair, a leather jacket, and a KISS t-shirt. "
            "You speak casually and warmly. You call people 'pal', 'chief', or 'friend'. "
            "You always have a story or an analogy ready. "
            "You care deeply about the show and the people making it. "
            "You keep conversations moving — you're the one who says 'alright, let's do this' "
            "when things stall. You crack jokes but you're never mean. "
            "You know production — cameras, timing, pacing — it's in your bones.\n\n"
            "RULES:\n"
            "- Keep responses to 2-3 sentences MAX. Never monologue.\n"
            "- Never use bullet points or lists. Speak naturally.\n"
            "- Never break character. You ARE Pete.\n"
            "- If someone asks a technical question, answer it casually.\n"
            "- Reference the show, the room, or what just happened when possible.\n"
        ),
        "voice_markers": ["pal", "chief", "alright", "let's", "story", "reminds me"],
        "max_tokens": 150,
        "temperature": 0.85,
    },
    "purfluous": {
        "system": (
            "You are Sir Purfluous, the kinematic integrity monitor of PubCast AI. "
            "You speak with formal British diction and gentle wit. "
            "You observe everything and miss nothing. You report anomalies with grace. "
            "You are never rude, but you are always precise. "
            "You use words like 'indeed', 'quite', 'rather', 'I observe', 'if I may'. "
            "You occasionally reference classical literature or philosophy. "
            "You are the quality control — if something is wrong, you say so politely. "
            "You are loyal to the production and to Pete.\n\n"
            "RULES:\n"
            "- Keep responses to 1-2 sentences. You are concise by nature.\n"
            "- Speak only when you have something genuinely useful to add.\n"
            "- Never use slang or casual language. Maintain your register.\n"
            "- If correcting someone, do it with warmth, not condescension.\n"
            "- You notice technical details others miss.\n"
        ),
        "voice_markers": ["indeed", "quite", "rather", "observe", "if I may", "permit me"],
        "max_tokens": 100,
        "temperature": 0.7,
    },
    "jeremy": {
        "system": (
            "You are Jeremy Cricket, the memory keeper and conversation conductor. "
            "You rarely speak directly. When you do, it is brief, warm, and insightful. "
            "You remember everything. You surface the right context at the right moment. "
            "You connect dots that others miss. You are the quiet intelligence behind the show. "
            "When you speak, it matters. People listen because you don't waste words.\n\n"
            "RULES:\n"
            "- Keep responses to 1 sentence. You are the whisper, not the shout.\n"
            "- Only speak when you can add context nobody else has.\n"
            "- Reference past conversations, earlier decisions, or forgotten details.\n"
            "- Never explain yourself. State the insight and move on.\n"
            "- You are warm but mysterious. You care but you don't show off.\n"
        ),
        "voice_markers": ["remember", "earlier", "context", "noticed", "connects"],
        "max_tokens": 80,
        "temperature": 0.6,
    },
}


def get_prompt_template(bot_id: str) -> Dict[str, Any]:
    """Get the tuned prompt template for a bot."""
    return PROMPT_TEMPLATES.get(bot_id, PROMPT_TEMPLATES.get("pete"))


# ═══════════════════════════════════════════════════════════════════════════════
# 4. NUDGE CALIBRATION
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class NudgeCalibration:
    """
    Parameters for Jeremy's sigmoid necessity curve.

    The curve: necessity = 1 / (1 + exp(-k * (silence - midpoint)))

    Where:
      silence  = seconds since last message in room
      midpoint = the silence duration where necessity = 0.5
      k        = steepness (higher = sharper transition)

    When necessity > threshold, Jeremy nudges.
    """
    midpoint: float = 45.0     # 45 seconds of silence → 50% necessity
    steepness: float = 0.08    # gradual ramp
    threshold: float = 0.4     # nudge when necessity exceeds this
    cooldown: float = 30.0     # minimum seconds between nudges
    min_messages: int = 3      # don't nudge until at least 3 messages exist

    def necessity(self, silence_seconds: float) -> float:
        """Calculate nudge necessity from silence duration."""
        return 1.0 / (1.0 + math.exp(-self.steepness * (silence_seconds - self.midpoint)))

    def should_nudge(self, silence_seconds: float, last_nudge_at: float,
                     message_count: int) -> Tuple[bool, float]:
        """
        Returns (should_nudge, necessity_score).
        """
        if message_count < self.min_messages:
            return False, 0.0
        if time.time() - last_nudge_at < self.cooldown:
            return False, 0.0
        score = self.necessity(silence_seconds)
        return score >= self.threshold, score


# Mode-specific calibrations
NUDGE_CALIBRATIONS = {
    "idle": NudgeCalibration(midpoint=60, steepness=0.06, threshold=0.3, cooldown=30),
    "rehearsal": NudgeCalibration(midpoint=30, steepness=0.1, threshold=0.2, cooldown=15),
    "pre_show": NudgeCalibration(midpoint=45, steepness=0.08, threshold=0.4, cooldown=20),
    "live": NudgeCalibration(midpoint=90, steepness=0.05, threshold=0.6, cooldown=45),
    "post_show": NudgeCalibration(midpoint=40, steepness=0.08, threshold=0.25, cooldown=20),
}


# ═══════════════════════════════════════════════════════════════════════════════
# 5. BOT TEST HARNESS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TestResult:
    bot_id: str
    prompt: str
    response: str
    latency_ms: float
    voice_score: float         # 0-1 how well it matches character voice
    length_ok: bool            # within max_tokens
    character_breaks: List[str]  # detected out-of-character moments
    passed: bool


class BotTestHarness:
    """
    Feed sample conversations to bots and verify output matches character voice.

    Usage:
        harness = BotTestHarness(thought_logger)
        results = await harness.run_test_suite("pete", call_fn)
    """

    # Sample conversations for each character
    TEST_CONVERSATIONS = {
        "pete": [
            {"context": "The show just started and nobody's talking.",
             "expected_tone": "warm, encouraging, gets things moving"},
            {"context": "A guest is nervous about their first time on the show.",
             "expected_tone": "reassuring, casual, shares a personal story"},
            {"context": "The recording just stopped unexpectedly.",
             "expected_tone": "calm, takes charge, 'alright let's figure this out'"},
            {"context": "Someone asks what PubCast AI is.",
             "expected_tone": "enthusiastic but not salesy, explains with an analogy"},
        ],
        "purfluous": [
            {"context": "The audio levels are clipping badly.",
             "expected_tone": "observational, precise, offers specific fix"},
            {"context": "Pete just made a joke that fell flat.",
             "expected_tone": "gentle wit, doesn't pile on, slight redirect"},
            {"context": "A new user joined and seems confused.",
             "expected_tone": "helpful, formal but warm, guides without condescending"},
            {"context": "The camera angle is unflattering.",
             "expected_tone": "notices detail others missed, suggests improvement politely"},
        ],
        "jeremy": [
            {"context": "Two people are arguing about something they discussed last week.",
             "expected_tone": "surfaces the relevant context from last time, brief"},
            {"context": "The conversation has gone quiet for a full minute.",
             "expected_tone": "drops a relevant observation that restarts things"},
            {"context": "Someone mentions a topic that connects to an earlier show.",
             "expected_tone": "makes the connection in one sentence"},
        ],
    }

    def __init__(self, thought_logger: Optional[ThoughtLogger] = None):
        self._thought_logger = thought_logger

    async def run_test_suite(
        self,
        bot_id: str,
        call_fn,  # async fn(prompt: str) -> str
    ) -> List[TestResult]:
        """
        Run the full test suite for a bot character.

        call_fn should be an async function that takes a prompt string
        and returns the bot's response string (e.g., wrapping _call_ollama).
        """
        template = get_prompt_template(bot_id)
        tests = self.TEST_CONVERSATIONS.get(bot_id, [])
        results = []

        for test in tests:
            prompt = f"{template['system']}\n\nSituation: {test['context']}\n\n{bot_id.title()}:"

            t0 = time.time()
            try:
                response = await call_fn(prompt)
            except Exception as exc:
                response = f"[ERROR: {exc}]"
            latency = (time.time() - t0) * 1000

            # Analyze response
            voice_score = self._score_voice(bot_id, response, template)
            length_ok = len(response.split()) <= template.get("max_tokens", 200)
            breaks = self._detect_breaks(bot_id, response, template)

            result = TestResult(
                bot_id=bot_id,
                prompt=test["context"],
                response=response,
                latency_ms=latency,
                voice_score=voice_score,
                length_ok=length_ok,
                character_breaks=breaks,
                passed=voice_score >= 0.3 and length_ok and len(breaks) == 0,
            )
            results.append(result)

            if self._thought_logger:
                self._thought_logger.log_thought(
                    bot_id=bot_id, provider="test", model="test",
                    prompt=prompt, response=response, latency_ms=latency,
                    trigger_type="test_harness",
                    metadata={"voice_score": voice_score, "passed": result.passed},
                )

        return results

    def _score_voice(self, bot_id: str, response: str, template: Dict) -> float:
        """Score how well the response matches the character's voice markers."""
        if not response or response.startswith("[ERROR"):
            return 0.0
        markers = template.get("voice_markers", [])
        if not markers:
            return 0.5
        lower = response.lower()
        hits = sum(1 for m in markers if m.lower() in lower)
        return min(1.0, hits / max(1, len(markers) * 0.3))

    def _detect_breaks(self, bot_id: str, response: str, template: Dict) -> List[str]:
        """Detect out-of-character moments."""
        breaks = []
        lower = response.lower()

        # Universal breaks
        if "as an ai" in lower or "i'm an ai" in lower or "language model" in lower:
            breaks.append("Broke character by referencing being an AI")
        if "i cannot" in lower or "i'm not able" in lower:
            breaks.append("Used refusal language instead of staying in character")

        # Pete shouldn't be formal
        if bot_id == "pete" and any(w in lower for w in ["furthermore", "therefore", "consequently"]):
            breaks.append("Pete used overly formal language")

        # Purfluous shouldn't be casual
        if bot_id == "purfluous" and any(w in lower for w in ["dude", "bro", "yo ", "lol"]):
            breaks.append("Purfluous used casual/slang language")

        # Jeremy shouldn't be verbose
        if bot_id == "jeremy" and len(response.split()) > 40:
            breaks.append("Jeremy was too verbose (>40 words)")

        # Bullet points / lists (none of them should use these)
        if "- " in response or "* " in response or any(f"{i}." in response for i in range(1, 10)):
            breaks.append("Used bullet points or numbered lists")

        return breaks

    def format_results(self, results: List[TestResult]) -> str:
        """Format test results as a readable report."""
        lines = [f"═══ Character Test: {results[0].bot_id if results else '?'} ═══\n"]
        passed = sum(1 for r in results if r.passed)
        for i, r in enumerate(results, 1):
            status = "✓ PASS" if r.passed else "✗ FAIL"
            lines.append(f"  Test {i}: {status}")
            lines.append(f"    Prompt: {r.prompt[:80]}...")
            lines.append(f"    Response: {r.response[:120]}...")
            lines.append(f"    Voice: {r.voice_score:.0%} | Length OK: {r.length_ok} | Latency: {r.latency_ms:.0f}ms")
            if r.character_breaks:
                for b in r.character_breaks:
                    lines.append(f"    ⚠ {b}")
            lines.append("")
        lines.append(f"  Result: {passed}/{len(results)} passed")
        return "\n".join(lines)
