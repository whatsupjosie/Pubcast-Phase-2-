# PubCast AI v5.0

**Collaborative AI-Infused Virtual Production**

*"The room stays the same. The window keeps changing."*

A virtual production studio where humans and AI characters create together. Step inside, take a seat, and make something nobody's ever seen before.

---

## Quick Start

```bash
# Clone and install
git clone <your-repo>
cd pubcast
pip install -r requirements.txt

# Start (Ollama optional but recommended)
python main.py

# Open browser
# → http://localhost:8000        (Lobby)
# → http://localhost:8000/static/waiting_room.html  (Airlock)
# → http://localhost:8000/static/stage.html          (3D Studio)
# → http://localhost:8000/static/control_room.html   (Director Console)
```

### With Docker

```bash
docker-compose up --build
# → Starts PubCast + Ollama together
# → Pull a model: docker exec -it pubcast-ollama-1 ollama pull mistral
```

---

## Architecture

PubCast boots 10 systems in dependency order:

| # | System | Purpose |
|---|--------|---------|
| 1 | Hub | WebSocket message router, chat, presence |
| 2 | RoomManager | 8 default rooms (studio, green room, control, etc.) |
| 3 | InferenceManager | Ollama local LLM connection |
| 4 | CricketKeeper | Per-character SQLite memory |
| 5 | BotManager | AI co-hosts (Pete, Purfluous, Jeremy Cricket) |
| 6 | Cameras + Recording | 6 sources, 4 encoding profiles, FFmpeg |
| 7 | Governance | Bans, avatar freeze, consent, waiting room |
| 8 | ThinkingContext | Jeremy conductor — room watcher, whisper engine |
| 9 | Ethereal Avatars | 57-joint skeleton, 7 neon colors |
| 10 | Vault | OS-level file immutability, shadow backup |

Everything degrades gracefully. If Ollama isn't running, bots go quiet. If ThinkingContext isn't installed, Jeremy doesn't nudge. Nothing crashes.

---

## AI Characters

**Pete** — The director. Pink hair, leather jacket, KISS shirt. Calls you "pal." Keeps things moving. Auto-replies to conversation.

**Sir Purfluous** — The quality monitor. Formal British diction. Notices everything. Speaks only when mentioned or when something is wrong.

**Jeremy Cricket** — The memory keeper. Rarely speaks. When he does, it's one sentence of pure insight. Works behind the scenes, watching rooms, nudging bots.

---

## Keyboard Shortcuts (Stage & Control Room)

| Key | Action |
|-----|--------|
| 1-5 | Switch camera |
| Space | Cut (swap program/preview) |
| R | Toggle recording |
| Escape | PANIC — fade to black, mute all |
| M | Mute all audio |
| D | Cycle AI Director mode |

---

## API Endpoints

### Production
- `GET /health` — System status
- `GET /api/cameras` — List cameras with status
- `POST /api/cameras/switch` — Switch program/preview
- `POST /api/cameras/cut` — CUT transition
- `POST /api/recording/start` — Start recording session
- `POST /api/recording/{id}/stop` — Stop recording
- `POST /api/production/panic` — Emergency stop

### Governance
- `POST /api/governance/consent` — Record user consent
- `POST /api/governance/waiting-room/request` — Request entry
- `POST /api/governance/ban` — Ban user
- `POST /api/governance/freeze` — Freeze avatar

### Avatars
- `GET /api/avatars/ethereal/colors` — Neon color palette
- `POST /api/avatars/ethereal/create` — Create ethereal skin
- `POST /api/avatars/ethereal/{id}/color` — Change color

### Vault
- `POST /api/vault/open` — Open vault (requires typed OPEN command)
- `POST /api/vault/files` — Protect a file
- `GET /api/vault/integrity` — Integrity report

---

## Configuration

Environment variables (all optional):

| Variable | Default | Description |
|----------|---------|-------------|
| PUBCAST_HOST | 0.0.0.0 | Server bind address |
| PUBCAST_PORT | 8000 | Server port |
| PUBCAST_DEBUG | 0 | Enable debug mode |
| PUBCAST_DATA_DIR | data | Data storage path |
| OLLAMA_HOST | http://localhost:11434 | Ollama server |
| OLLAMA_MODEL | mistral | Default LLM model |

---

## Iteration Wallet (Standalone)

```bash
python iteration_wallet_portable.py
# → http://localhost:8765
```

Hardened file protection vault that works independently of PubCast. OS-level immutability flags, shadow backup with auto-restore, hash-chain audit log.

---

## Testing

```bash
python -m pytest tests/test_pubcast.py -v
# 28 tests covering all systems
```

---

## License

© 2024–2026 Rear View Foresight LLC. All rights reserved.

*Feic Mo Chroí — See My Heart*
