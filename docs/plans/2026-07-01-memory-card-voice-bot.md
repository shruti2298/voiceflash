# Memory Card Voice Bot Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a voice-based Memory Card game bot: it speaks a growing sequence of words, the user repeats them back over a live voice call, and the backend deterministically validates the answer, scores it, persists everything, and progresses the game.

**Architecture:** One Python process (FastAPI) does three jobs: (1) serves REST APIs for sessions/state/scores/leaderboard, (2) hosts the Pipecat voice pipeline over browser WebRTC, and (3) serves a minimal web UI. The **game "brain" lives in one place** — a pure `engine.py` (sequence generation, normalization, comparison, scoring, difficulty) wrapped by a `GameService` that adds Postgres persistence + an in-memory cache. Both the REST routes and the Pipecat voice processor call the *same* `GameService`, so there is a single source of truth and no duplicated logic. Validation is 100% code, never the LLM. The LLM (Groq) is used only to phrase engaging game-host lines.

**Tech Stack:** Python 3.11 · Pipecat (SmallWebRTC transport, Deepgram STT+TTS, Groq LLM) · FastAPI · SQLAlchemy + PostgreSQL (Docker) · cachetools (in-memory) · vanilla HTML/JS · pytest.

---

## Architecture at a glance

```
                 ┌──────────────────────── Browser (static/index.html) ───────────────────────┐
                 │  mic/audio  ──WebRTC──►                            ◄── polls GET /api/... ──  │
                 └───────────────┬───────────────────────────────────────────┬─────────────────┘
                                 │ audio                                       │ JSON
                    ┌────────────▼─────────────┐               ┌──────────────▼──────────────┐
                    │   Pipecat pipeline        │               │      FastAPI REST routes     │
                    │  STT → GameProcessor → TTS│               │  /sessions /state /end /lb   │
                    │  (Deepgram) (Groq for     │               └──────────────┬──────────────┘
                    │   host banter) (Deepgram) │                              │
                    └────────────┬─────────────┘                              │
                                 │            both call the SAME service       │
                                 └───────────────┬──────────────┬─────────────┘
                                                 ▼              ▼
                                        ┌─────────────┐  ┌──────────────┐
                                        │ GameService │  │ in-mem cache │  active session state
                                        │  + engine   │  │ (cachetools) │  + leaderboard
                                        └──────┬──────┘  └──────────────┘
                                               ▼
                                        ┌─────────────┐
                                        │ PostgreSQL  │  sessions, rounds, responses, scores
                                        └─────────────┘
```

## Final directory layout

```
voiceflash/
├─ docker-compose.yml          # postgres
├─ .env.example                # config template
├─ requirements.txt            # PYTHON deps (root) — note: assignment brief stays at src/requirements.txt
├─ README.md
├─ app/
│  ├─ __init__.py
│  ├─ main.py                  # FastAPI app: mounts API + webrtc + static
│  ├─ config.py               # env-driven settings
│  ├─ schemas.py              # Pydantic request/response DTOs
│  ├─ db/
│  │  ├─ __init__.py
│  │  ├─ database.py          # engine + SessionLocal + Base
│  │  └─ models.py            # GameSession, Round, Response
│  ├─ cache/
│  │  ├─ __init__.py
│  │  └─ store.py             # TTLCache wrappers (active session, leaderboard)
│  ├─ game/
│  │  ├─ __init__.py
│  │  ├─ words.py             # seeded word list
│  │  ├─ engine.py            # PURE logic (no DB, no cache)
│  │  └─ service.py           # GameService = engine + persistence + cache
│  ├─ api/
│  │  ├─ __init__.py
│  │  └─ routes.py            # REST endpoints
│  └─ voice/
│     ├─ __init__.py
│     ├─ bot.py               # Pipecat pipeline assembly
│     ├─ game_processor.py    # custom FrameProcessor that drives the game
│     └─ webrtc.py            # SmallWebRTC signaling route
├─ static/
│  ├─ index.html
│  ├─ app.js
│  └─ style.css
└─ tests/
   ├─ __init__.py
   ├─ conftest.py
   ├─ test_engine.py
   ├─ test_cache.py
   ├─ test_service.py
   └─ test_api.py
```

## Requirements → where it's satisfied (traceability)

| Requirement (src/requirements.txt) | Where |
|---|---|
| Pipecat voice pipeline | `app/voice/bot.py` |
| Speak sequence, listen, validate, update, next/end | `app/voice/game_processor.py` + `GameService` |
| Turn-taking (wait until user finishes) | VAD + per-turn transcript aggregation in `game_processor.py` |
| Interruption handling / barge-in recovery | Pipecat interruptions + `StartInterruptionFrame` handling |
| Human-like game-host behavior | Groq LLM for banter (facts come from engine) |
| Persist session/round/response/score | `app/db/models.py` + `GameService` |
| Backend APIs (start/state/end/leaderboard) | `app/api/routes.py` |
| Caching ≥1 meaningful flow | `app/cache/store.py` (active session + leaderboard) |
| Avoid double-scoring | idempotent `submit_answer` + DB unique constraint on `round_id` |
| Validation in code, not LLM | `app/game/engine.py` |
| Word list hardcoded/seeded | `app/game/words.py` |

## How you'll run it (final state — for reference)

```bash
docker compose up -d                      # start postgres
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                       # fill DEEPGRAM_API_KEY + GROQ_API_KEY
uvicorn app.main:app --reload --port 8000
# open http://localhost:8000
```

---

## Game rules (deterministic — implemented in engine.py)

- A **sequence** is an ordered list of simple words drawn from a seeded pool.
- **Difficulty ramp:** round *n* sequence length = `min(2 + n, MAX_LEN)` → round 1 = 3 words, round 2 = 4, … capped at `MAX_LEN` (default 8).
- Bot speaks the sequence → user repeats → engine **normalizes** the transcript (lowercase, strip punctuation, drop filler words) and compares token-by-token, in order.
- **Correct:** award `10 × sequence_length` points, advance to next round.
- **Incorrect:** game over; final score persisted. (Simple, classic single-life memory game.)
- **Idempotent scoring:** each round is answered exactly once; a repeat submission returns the stored result and never re-scores.

---

# Phase 0 — Project setup

### Task 0.1: Initialize git and clean up Java scaffolding

**Files:**
- Delete: `pom.xml`, `target/`, `src/main/`, `.mvn/`
- Keep: `src/requirements.txt` (the assignment brief — leave as reference)

**Step 1:** Initialize git (repo is currently not a git repo):

```bash
cd /Users/shrutipatolia/IdeaProjects/voiceflash
git init
```

**Step 2:** Remove the Java project files (we are all-Python now):

```bash
rm -rf pom.xml target src/main .mvn
```

**Step 3:** Verify the assignment brief is still present:

```bash
test -f src/requirements.txt && echo "brief kept OK"
```
Expected: `brief kept OK`

**Step 4:** Commit.

```bash
git add -A
git commit -m "chore: remove java scaffolding, init python repo"
```

### Task 0.2: Python 3.11 virtualenv

> System Python is 3.9.3; **Pipecat requires 3.10+**. Use 3.11.

**Step 1:** Install Python 3.11 if missing (macOS):

```bash
brew install python@3.11
```

**Step 2:** Create and activate the venv:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python --version
```
Expected: `Python 3.11.x`

### Task 0.3: requirements.txt (Python deps) + .gitignore

**Files:**
- Create: `requirements.txt`

**Step 1:** Write `requirements.txt`:

```
# web / api
fastapi==0.115.*
uvicorn[standard]==0.32.*
pydantic==2.*
pydantic-settings==2.*

# db
sqlalchemy==2.*
psycopg2-binary==2.9.*

# cache
cachetools==5.*

# voice (Pipecat + services). Pin to a known-good recent release.
pipecat-ai[deepgram,groq,silero,webrtc]==0.0.*

# dev/test
pytest==8.*
httpx==0.27.*        # FastAPI TestClient
python-dotenv==1.*
```

> **Note for implementer:** Pipecat's public API and its **extras names** evolve between 0.0.x releases — the `[deepgram,groq,silero,webrtc]` extras above may not all resolve on the version pip picks. If `pip install` errors on an unknown extra, check the version's `pyproject.toml`/PyPI page for the correct names (e.g. the WebRTC extra is sometimes `webrtc`, sometimes bundled elsewhere). After installing, pin the exact version (`pip freeze | grep pipecat`).

**Step 2:** Install:

```bash
pip install -r requirements.txt
```

**Step 3: Fail fast — verify the voice imports resolve on the installed version** (do this now, not in Phase 7, so setup problems surface on day one):

```bash
python - <<'PY'
from pipecat.pipeline.pipeline import Pipeline
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.transports.network.small_webrtc import SmallWebRTCTransport
from pipecat.audio.vad.silero import SileroVADAnalyzer
print("pipecat imports OK")
PY
```
Expected: `pipecat imports OK`. If any import fails, find the correct path in the installed package (`python -c "import pipecat, os; print(os.path.dirname(pipecat.__file__))"` then browse the tree) and update the imports in `bot.py`/`webrtc.py`/`game_processor.py` accordingly before proceeding.

**Step 4:** Append Python entries to `.gitignore`:

```
.venv/
__pycache__/
*.pyc
.env
game.db
.pytest_cache/
```

**Step 5:** Commit.

```bash
git add requirements.txt .gitignore
git commit -m "chore: python dependencies and gitignore"
```

### Task 0.4: docker-compose for Postgres + .env.example

**Files:**
- Create: `docker-compose.yml`, `.env.example`

**Step 1:** `docker-compose.yml`:

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: voiceflash
      POSTGRES_PASSWORD: voiceflash
      POSTGRES_DB: voiceflash
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U voiceflash"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  pgdata:
```

**Step 2:** `.env.example`:

```
DATABASE_URL=postgresql+psycopg2://voiceflash:voiceflash@localhost:5432/voiceflash
DEEPGRAM_API_KEY=your_deepgram_key_here
GROQ_API_KEY=your_groq_key_here
GROQ_MODEL=llama-3.1-8b-instant
MAX_SEQUENCE_LENGTH=8
```

**Step 3:** Start Postgres and verify:

```bash
docker compose up -d
docker compose ps
```
Expected: `db` service `healthy`.

**Step 4:** Commit.

```bash
git add docker-compose.yml .env.example
git commit -m "chore: postgres via docker-compose and env template"
```

### Task 0.5: config + package skeleton

**Files:**
- Create: `app/__init__.py`, `app/config.py`, and empty `__init__.py` in every package dir; `tests/__init__.py`.

**Step 1:** Create package markers:

```bash
mkdir -p app/db app/cache app/game app/api app/voice static tests
touch app/__init__.py app/db/__init__.py app/cache/__init__.py \
      app/game/__init__.py app/api/__init__.py app/voice/__init__.py \
      tests/__init__.py
```

**Step 2:** `app/config.py`:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://voiceflash:voiceflash@localhost:5432/voiceflash"
    deepgram_api_key: str = ""
    groq_api_key: str = ""
    groq_model: str = "llama-3.1-8b-instant"
    max_sequence_length: int = 8


settings = Settings()
```

**Step 3:** Commit.

```bash
git add app tests
git commit -m "chore: package skeleton and config"
```

---

# Phase 1 — Database models

### Task 1.1: SQLAlchemy engine/session/base

**Files:**
- Create: `app/db/database.py`

**Step 1:** Write it:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    """FastAPI dependency: yields a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

**Step 2:** Commit.

```bash
git add app/db/database.py
git commit -m "feat: sqlalchemy database setup"
```

### Task 1.2: ORM models

**Files:**
- Create: `app/db/models.py`

**Step 1:** Write the models (note the **unique constraint on `round_id`** in `Response` — this is the DB-level guard against double-scoring):

```python
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, JSON, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GameSession(Base):
    __tablename__ = "game_sessions"

    id = Column(String, primary_key=True, default=_uuid)
    player_name = Column(String, nullable=False)
    status = Column(String, nullable=False, default="ACTIVE")   # ACTIVE | ENDED
    score = Column(Integer, nullable=False, default=0)
    current_round = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=_now)
    ended_at = Column(DateTime(timezone=True), nullable=True)

    rounds = relationship("Round", back_populates="session", cascade="all, delete-orphan")


class Round(Base):
    __tablename__ = "rounds"

    id = Column(String, primary_key=True, default=_uuid)
    session_id = Column(String, ForeignKey("game_sessions.id"), nullable=False)
    round_number = Column(Integer, nullable=False)
    sequence = Column(JSON, nullable=False)                     # e.g. ["apple","tiger","river"]
    status = Column(String, nullable=False, default="PENDING")  # PENDING | CORRECT | WRONG
    created_at = Column(DateTime(timezone=True), default=_now)

    session = relationship("GameSession", back_populates="rounds")
    response = relationship("Response", back_populates="round", uselist=False,
                            cascade="all, delete-orphan")


class Response(Base):
    __tablename__ = "responses"
    __table_args__ = (UniqueConstraint("round_id", name="uq_response_per_round"),)

    id = Column(String, primary_key=True, default=_uuid)
    round_id = Column(String, ForeignKey("rounds.id"), nullable=False)
    transcript = Column(String, nullable=False)
    normalized = Column(JSON, nullable=False)                   # tokenized answer
    is_correct = Column(Boolean, nullable=False)
    points_awarded = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=_now)

    round = relationship("Round", back_populates="response")
```

**Step 2:** Commit.

```bash
git add app/db/models.py
git commit -m "feat: game session/round/response models"
```

---

# Phase 2 — Seeded word list

### Task 2.1: Word pool

**Files:**
- Create: `app/game/words.py`

**Step 1:** Write it:

```python
# Simple, phonetically-distinct, TTS/STT-friendly concrete nouns.
WORDS = [
    "apple", "tiger", "river", "guitar", "planet", "candle", "rocket", "garden",
    "monkey", "pencil", "orange", "dragon", "castle", "silver", "yellow", "window",
    "mountain", "diamond", "pumpkin", "butterfly", "helicopter", "umbrella",
    "elephant", "chocolate", "computer", "sunflower",
]
```

**Step 2:** Commit.

```bash
git add app/game/words.py
git commit -m "feat: seeded word pool"
```

---

# Phase 3 — Game engine (pure logic, TDD)

> This is the deterministic heart. No DB, no cache, no network — trivially testable.

### Task 3.1: sequence length + generation

**Files:**
- Create: `app/game/engine.py`
- Test: `tests/test_engine.py`

**Step 1: Write the failing test.**

```python
# tests/test_engine.py
from app.game import engine


def test_sequence_length_grows_and_caps():
    assert engine.sequence_length(1, max_len=8) == 3
    assert engine.sequence_length(2, max_len=8) == 4
    assert engine.sequence_length(100, max_len=8) == 8


def test_generate_sequence_has_correct_length_and_valid_words():
    seq = engine.generate_sequence(round_number=3, max_len=8)
    assert len(seq) == 5
    from app.game.words import WORDS
    assert all(w in WORDS for w in seq)


def test_generate_sequence_never_exceeds_word_pool():
    from app.game.words import WORDS
    # even with an absurd max_len, sampling must not raise ValueError
    seq = engine.generate_sequence(round_number=1000, max_len=10_000)
    assert len(seq) == len(WORDS)
```

**Step 2: Run — expect fail.**

```bash
pytest tests/test_engine.py -v
```
Expected: FAIL (`module 'app.game.engine' has no attribute 'sequence_length'`).

**Step 3: Implement.**

```python
# app/game/engine.py
import random
import re
from dataclasses import dataclass
from typing import List

from app.game.words import WORDS

FILLER = {"um", "uh", "the", "a", "an", "and", "then", "was", "is", "please", "okay"}


def sequence_length(round_number: int, max_len: int = 8) -> int:
    return min(2 + round_number, max_len)


def generate_sequence(round_number: int, max_len: int = 8) -> List[str]:
    # cap length at the pool size so random.sample never raises ValueError
    # even if MAX_SEQUENCE_LENGTH is misconfigured larger than the word pool
    n = min(sequence_length(round_number, max_len), len(WORDS))
    # sample without replacement so the sequence has no repeats (easier to say/hear)
    return random.sample(WORDS, n)
```

**Step 4: Run — expect pass.**

```bash
pytest tests/test_engine.py -v
```
Expected: PASS.

**Step 5: Commit.**

```bash
git add app/game/engine.py tests/test_engine.py
git commit -m "feat: engine sequence generation"
```

### Task 3.2: transcript normalization

**Step 1: Add failing tests** to `tests/test_engine.py`:

```python
def test_normalize_lowercases_strips_punctuation_and_fillers():
    assert engine.normalize("Apple, tiger and river!") == ["apple", "tiger", "river"]

def test_normalize_drops_leading_filler_phrases():
    assert engine.normalize("um the answer is apple tiger") == ["answer", "apple", "tiger"]
```

**Step 2: Run — expect fail.**

```bash
pytest tests/test_engine.py -k normalize -v
```
Expected: FAIL.

**Step 3: Implement** — add to `engine.py`:

```python
def normalize(text: str) -> List[str]:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)          # drop punctuation
    tokens = [t for t in text.split() if t and t not in FILLER]
    return tokens
```

**Step 4: Run — expect pass.**

```bash
pytest tests/test_engine.py -k normalize -v
```
Expected: PASS.

**Step 5: Commit.**

```bash
git add app/game/engine.py tests/test_engine.py
git commit -m "feat: engine transcript normalization"
```

### Task 3.3: evaluation + scoring

**Step 1: Add failing tests:**

```python
def test_evaluate_correct_answer():
    r = engine.evaluate(["apple", "tiger"], "Apple, tiger!")
    assert r.is_correct is True
    assert r.points == 20            # 10 * length(2)
    assert r.expected == ["apple", "tiger"]

def test_evaluate_wrong_order_is_incorrect():
    r = engine.evaluate(["apple", "tiger"], "tiger apple")
    assert r.is_correct is False
    assert r.points == 0

def test_evaluate_wrong_word_is_incorrect():
    r = engine.evaluate(["apple", "tiger"], "apple river")
    assert r.is_correct is False
```

**Step 2: Run — expect fail.**

```bash
pytest tests/test_engine.py -k evaluate -v
```
Expected: FAIL.

**Step 3: Implement** — add to `engine.py`:

```python
@dataclass
class Evaluation:
    is_correct: bool
    points: int
    expected: List[str]
    heard: List[str]


def score(sequence_length_: int) -> int:
    return 10 * sequence_length_


def evaluate(expected: List[str], transcript: str) -> Evaluation:
    heard = normalize(transcript)
    is_correct = heard == expected
    return Evaluation(
        is_correct=is_correct,
        points=score(len(expected)) if is_correct else 0,
        expected=expected,
        heard=heard,
    )
```

**Step 4: Run — expect pass.**

```bash
pytest tests/test_engine.py -v
```
Expected: all PASS.

**Step 5: Commit.**

```bash
git add app/game/engine.py tests/test_engine.py
git commit -m "feat: engine evaluation and scoring"
```

---

# Phase 4 — Cache layer (TDD)

### Task 4.1: TTL cache wrappers

**Files:**
- Create: `app/cache/store.py`
- Test: `tests/test_cache.py`

**Step 1: Write failing test.**

```python
# tests/test_cache.py
from app.cache import store


def test_active_session_roundtrip():
    store.clear_all()
    store.set_active_session("s1", {"score": 30})
    assert store.get_active_session("s1") == {"score": 30}

def test_missing_session_returns_none():
    store.clear_all()
    assert store.get_active_session("nope") is None

def test_leaderboard_cache_and_invalidate():
    store.clear_all()
    store.set_leaderboard([{"player_name": "a", "score": 50}])
    assert store.get_leaderboard()[0]["player_name"] == "a"
    store.invalidate_leaderboard()
    assert store.get_leaderboard() is None
```

**Step 2: Run — expect fail.**

```bash
pytest tests/test_cache.py -v
```
Expected: FAIL.

**Step 3: Implement.**

```python
# app/cache/store.py
"""In-memory caches (cachetools). Two meaningful flows:
   - active session state: read on every voice turn + GET /state (hot path)
   - leaderboard: short-TTL cache, invalidated when a session ends
"""
from typing import Any, Optional

from cachetools import TTLCache

# up to 1000 active sessions, expire 30 min after last write
_active_sessions: TTLCache = TTLCache(maxsize=1000, ttl=30 * 60)
# single leaderboard snapshot, 60s freshness
_leaderboard: TTLCache = TTLCache(maxsize=1, ttl=60)

_LB_KEY = "leaderboard"


def get_active_session(session_id: str) -> Optional[Any]:
    return _active_sessions.get(session_id)


def set_active_session(session_id: str, state: Any) -> None:
    _active_sessions[session_id] = state


def drop_active_session(session_id: str) -> None:
    _active_sessions.pop(session_id, None)


def get_leaderboard() -> Optional[Any]:
    return _leaderboard.get(_LB_KEY)


def set_leaderboard(rows: Any) -> None:
    _leaderboard[_LB_KEY] = rows


def invalidate_leaderboard() -> None:
    _leaderboard.pop(_LB_KEY, None)


def clear_all() -> None:
    _active_sessions.clear()
    _leaderboard.clear()
```

**Step 4: Run — expect pass.**

```bash
pytest tests/test_cache.py -v
```
Expected: PASS.

**Step 5: Commit.**

```bash
git add app/cache/store.py tests/test_cache.py
git commit -m "feat: in-memory cache for session state and leaderboard"
```

---

# Phase 5 — GameService (orchestration: engine + persistence + cache, TDD)

> This is the layer both the API and the voice bot call. It owns the game flow.

### Task 5.1: DTO + service scaffolding + test fixtures

**Files:**
- Create: `app/schemas.py`, `app/game/service.py`
- Create: `tests/conftest.py`

**Step 1:** `app/schemas.py` (Pydantic DTOs the API returns):

```python
from typing import List, Optional
from pydantic import BaseModel


class StartSessionRequest(BaseModel):
    player_name: str


class SubmitAnswerRequest(BaseModel):
    transcript: str


class SessionState(BaseModel):
    session_id: str
    player_name: str
    status: str
    score: int
    current_round: int
    round_id: Optional[str] = None
    sequence_length: Optional[int] = None   # length only; never leak the words to the UI


class AnswerResult(BaseModel):
    session_id: str
    round_number: int
    is_correct: bool
    points_awarded: int
    total_score: int
    status: str                              # ACTIVE (advanced) | ENDED (game over)
    expected: List[str]                      # revealed after answering, for feedback
    heard: List[str]


class LeaderboardEntry(BaseModel):
    player_name: str
    score: int
```

**Step 2:** `tests/conftest.py` — an isolated SQLite DB per test module so service tests don't need Postgres running (production still uses Postgres via `DATABASE_URL`):

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, future=True)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
```

> **Note:** `JSON` and the models are SQLite-compatible, so the same models test cleanly in-memory.

**Step 3:** Create empty `app/game/service.py`:

```python
# app/game/service.py  (implemented in the next tasks)
```

**Step 4:** Commit.

```bash
git add app/schemas.py app/game/service.py tests/conftest.py
git commit -m "chore: schemas, service stub, test db fixture"
```

### Task 5.2: start_session

**Files:**
- Modify: `app/game/service.py`
- Test: `tests/test_service.py`

**Step 1: Write failing test.**

```python
# tests/test_service.py
from app.game.service import GameService
from app.cache import store


def test_start_session_creates_round_one(db):
    store.clear_all()
    svc = GameService(db)
    state = svc.start_session("Alice")
    assert state.player_name == "Alice"
    assert state.status == "ACTIVE"
    assert state.current_round == 1
    assert state.score == 0
    assert state.sequence_length == 3        # round 1 = 3 words
    assert state.round_id is not None
    # cache is warmed
    assert store.get_active_session(state.session_id) is not None
```

**Step 2: Run — expect fail.**

```bash
pytest tests/test_service.py -k start_session -v
```
Expected: FAIL.

**Step 3: Implement** `service.py`:

```python
from typing import List, Optional

from sqlalchemy.orm import Session

from app.cache import store
from app.config import settings
from app.db import models
from app.game import engine
from app.schemas import AnswerResult, LeaderboardEntry, SessionState


class GameService:
    def __init__(self, db: Session):
        self.db = db

    # ---- helpers -------------------------------------------------------
    def _current_round(self, session: models.GameSession) -> Optional[models.Round]:
        return (
            self.db.query(models.Round)
            .filter(models.Round.session_id == session.id,
                    models.Round.round_number == session.current_round)
            .one_or_none()
        )

    def _state(self, session: models.GameSession) -> SessionState:
        rnd = self._current_round(session) if session.status == "ACTIVE" else None
        state = SessionState(
            session_id=session.id,
            player_name=session.player_name,
            status=session.status,
            score=session.score,
            current_round=session.current_round,
            round_id=rnd.id if rnd else None,
            sequence_length=len(rnd.sequence) if rnd else None,
        )
        return state

    def _new_round(self, session: models.GameSession) -> models.Round:
        seq = engine.generate_sequence(session.current_round, settings.max_sequence_length)
        rnd = models.Round(session_id=session.id, round_number=session.current_round,
                           sequence=seq, status="PENDING")
        self.db.add(rnd)
        self.db.flush()
        return rnd

    # ---- public API ----------------------------------------------------
    def start_session(self, player_name: str) -> SessionState:
        session = models.GameSession(player_name=player_name, status="ACTIVE",
                                     score=0, current_round=1)
        self.db.add(session)
        self.db.flush()
        self._new_round(session)
        self.db.commit()
        state = self._state(session)
        store.set_active_session(session.id, state.model_dump())
        return state
```

**Step 4: Run — expect pass.**

```bash
pytest tests/test_service.py -k start_session -v
```
Expected: PASS.

**Step 5: Commit.**

```bash
git add app/game/service.py tests/test_service.py
git commit -m "feat: GameService.start_session"
```

### Task 5.3: get_state (cache-first) + get_sequence (for the bot)

**Step 1: Add failing tests.**

```python
def test_get_state_reads_from_cache(db):
    store.clear_all()
    svc = GameService(db)
    started = svc.start_session("Bob")
    # mutate cache to prove get_state prefers it
    cached = store.get_active_session(started.session_id)
    cached["score"] = 999
    store.set_active_session(started.session_id, cached)
    assert svc.get_state(started.session_id).score == 999

def test_get_sequence_returns_words_for_bot(db):
    store.clear_all()
    svc = GameService(db)
    started = svc.start_session("Cara")
    seq = svc.get_current_sequence(started.session_id)
    assert len(seq) == 3
```

**Step 2: Run — expect fail.**

```bash
pytest tests/test_service.py -k "get_state or get_sequence" -v
```
Expected: FAIL.

**Step 3: Implement** — add to `GameService`:

```python
    def get_state(self, session_id: str) -> SessionState:
        cached = store.get_active_session(session_id)
        if cached is not None:
            return SessionState(**cached)
        session = self.db.get(models.GameSession, session_id)
        if session is None:
            raise KeyError(session_id)
        state = self._state(session)
        if session.status == "ACTIVE":
            store.set_active_session(session_id, state.model_dump())
        return state

    def get_current_sequence(self, session_id: str) -> List[str]:
        """Used by the voice bot only — never exposed via the public API."""
        session = self.db.get(models.GameSession, session_id)
        if session is None:
            raise KeyError(session_id)
        rnd = self._current_round(session)
        return list(rnd.sequence) if rnd else []
```

**Step 4: Run — expect pass.**

```bash
pytest tests/test_service.py -k "get_state or get_sequence" -v
```
Expected: PASS.

**Step 5: Commit.**

```bash
git add app/game/service.py tests/test_service.py
git commit -m "feat: GameService.get_state (cache-first) and get_current_sequence"
```

### Task 5.4: submit_answer (correct → advance) + idempotency (no double-scoring)

**Step 1: Add failing tests** — this is the most important behavior:

```python
def test_correct_answer_advances_and_scores(db):
    store.clear_all()
    svc = GameService(db)
    started = svc.start_session("Dee")
    seq = svc.get_current_sequence(started.session_id)
    res = svc.submit_answer(started.session_id, started.round_id, " ".join(seq))
    assert res.is_correct is True
    assert res.points_awarded == 30          # 10 * 3
    assert res.total_score == 30
    assert res.status == "ACTIVE"
    # advanced to round 2
    assert svc.get_state(started.session_id).current_round == 2

def test_wrong_answer_ends_game(db):
    store.clear_all()
    svc = GameService(db)
    started = svc.start_session("Eve")
    res = svc.submit_answer(started.session_id, started.round_id, "definitely wrong words")
    assert res.is_correct is False
    assert res.status == "ENDED"
    assert svc.get_state(started.session_id).status == "ENDED"

def test_double_submission_is_idempotent(db):
    store.clear_all()
    svc = GameService(db)
    started = svc.start_session("Foo")
    seq = svc.get_current_sequence(started.session_id)
    first = svc.submit_answer(started.session_id, started.round_id, " ".join(seq))
    # submit the SAME round again — must return the same result, not re-score
    again = svc.submit_answer(started.session_id, started.round_id, " ".join(seq))
    assert again.points_awarded == first.points_awarded
    assert again.total_score == first.total_score == 30   # not 60
```

**Step 2: Run — expect fail.**

```bash
pytest tests/test_service.py -k "answer or idempotent" -v
```
Expected: FAIL.

**Step 3: Implement** — add to `GameService`:

```python
    def submit_answer(self, session_id: str, round_id: str, transcript: str) -> AnswerResult:
        session = self.db.get(models.GameSession, session_id)
        if session is None:
            raise KeyError(session_id)
        rnd = self.db.get(models.Round, round_id)
        if rnd is None or rnd.session_id != session_id:
            raise KeyError(round_id)

        # --- idempotency guard: a round already answered returns its stored result ---
        if rnd.status != "PENDING" and rnd.response is not None:
            return self._result_from_stored(session, rnd)

        ev = engine.evaluate(list(rnd.sequence), transcript)

        response = models.Response(
            round_id=rnd.id, transcript=transcript, normalized=ev.heard,
            is_correct=ev.is_correct, points_awarded=ev.points,
        )
        self.db.add(response)
        rnd.status = "CORRECT" if ev.is_correct else "WRONG"

        if ev.is_correct:
            session.score += ev.points
            session.current_round += 1
            self.db.flush()
            self._new_round(session)            # prepare the next round
        else:
            session.status = "ENDED"
            from datetime import datetime, timezone
            session.ended_at = datetime.now(timezone.utc)

        self.db.commit()

        # refresh caches
        if session.status == "ACTIVE":
            store.set_active_session(session.id, self._state(session).model_dump())
        else:
            store.drop_active_session(session.id)
            store.invalidate_leaderboard()

        return AnswerResult(
            session_id=session.id, round_number=rnd.round_number,
            is_correct=ev.is_correct, points_awarded=ev.points,
            total_score=session.score, status=session.status,
            expected=ev.expected, heard=ev.heard,
        )

    def _result_from_stored(self, session: models.GameSession, rnd: models.Round) -> AnswerResult:
        resp = rnd.response
        return AnswerResult(
            session_id=session.id, round_number=rnd.round_number,
            is_correct=resp.is_correct, points_awarded=resp.points_awarded,
            total_score=session.score, status=session.status,
            expected=list(rnd.sequence), heard=list(resp.normalized),
        )
```

**Step 4: Run — expect pass.**

```bash
pytest tests/test_service.py -v
```
Expected: all PASS.

**Step 5: Commit.**

```bash
git add app/game/service.py tests/test_service.py
git commit -m "feat: GameService.submit_answer with idempotent scoring"
```

### Task 5.5: end_session + leaderboard (cached)

**Step 1: Add failing tests.**

```python
def test_end_session_marks_ended(db):
    store.clear_all()
    svc = GameService(db)
    started = svc.start_session("Gil")
    ended = svc.end_session(started.session_id)
    assert ended.status == "ENDED"

def test_leaderboard_orders_by_score_and_caches(db):
    store.clear_all()
    svc = GameService(db)
    for name, wrong in [("Hi", "x"), ("Jo", "y")]:
        s = svc.start_session(name)
        seq = svc.get_current_sequence(s.session_id)
        if name == "Jo":
            svc.submit_answer(s.session_id, s.round_id, " ".join(seq))  # Jo scores 30
        svc.end_session(s.session_id)
    lb = svc.leaderboard(limit=10)
    assert lb[0].player_name == "Jo"
    assert store.get_leaderboard() is not None      # now cached
```

**Step 2: Run — expect fail.**

```bash
pytest tests/test_service.py -k "end_session or leaderboard" -v
```
Expected: FAIL.

**Step 3: Implement** — add to `GameService`:

```python
    def end_session(self, session_id: str) -> SessionState:
        session = self.db.get(models.GameSession, session_id)
        if session is None:
            raise KeyError(session_id)
        if session.status == "ACTIVE":
            session.status = "ENDED"
            from datetime import datetime, timezone
            session.ended_at = datetime.now(timezone.utc)
            self.db.commit()
        store.drop_active_session(session_id)
        store.invalidate_leaderboard()
        return self._state(session)

    def leaderboard(self, limit: int = 10) -> List[LeaderboardEntry]:
        cached = store.get_leaderboard()
        if cached is not None:
            return [LeaderboardEntry(**row) for row in cached[:limit]]
        rows = (
            self.db.query(models.GameSession)
            .order_by(models.GameSession.score.desc(), models.GameSession.created_at.asc())
            .limit(limit)
            .all()
        )
        entries = [LeaderboardEntry(player_name=r.player_name, score=r.score) for r in rows]
        store.set_leaderboard([e.model_dump() for e in entries])
        return entries
```

**Step 4: Run — expect pass.**

```bash
pytest tests/test_service.py -v
```
Expected: all PASS.

**Step 5: Commit.**

```bash
git add app/game/service.py tests/test_service.py
git commit -m "feat: GameService.end_session and cached leaderboard"
```

---

# Phase 6 — REST API (FastAPI, TDD via TestClient)

### Task 6.1: routes

**Files:**
- Create: `app/api/routes.py`
- Test: `tests/test_api.py`

**Step 1:** Add a table-create-on-startup + app wiring stub `app/main.py` (minimal for now; extended in Phase 7):

```python
# app/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db.database import Base, engine
from app.api.routes import router as api_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Create tables on startup (dev convenience; use migrations for prod).
    # This lives in the lifespan handler — NOT at import time — so importing
    # app.main never touches Postgres. TestClient only fires the lifespan when
    # used as a context manager, so unit tests (which don't) stay Postgres-free.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Memory Card Voice Bot", lifespan=lifespan)
app.include_router(api_router, prefix="/api")
```

**Step 2: Write failing test** (uses SQLite override so no Postgres needed in CI):

```python
# tests/test_api.py
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base, get_db
from app.cache import store


@pytest.fixture()
def client():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, future=True)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    # Importing app.main is now Postgres-free: table creation lives in the
    # lifespan handler, and TestClient(app) below is NOT used as a context
    # manager, so the lifespan never fires. Tables were already created on the
    # SQLite engine above; the get_db override routes all queries there.
    from app.main import app
    app.dependency_overrides[get_db] = override_get_db
    store.clear_all()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_start_and_get_state(client):
    r = client.post("/api/sessions", json={"player_name": "Ann"})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    s = client.get(f"/api/sessions/{sid}")
    assert s.status_code == 200
    assert s.json()["current_round"] == 1
    # the words are never leaked, only the length
    assert "sequence" not in s.json()
    assert s.json()["sequence_length"] == 3


def test_end_session_and_leaderboard(client):
    sid = client.post("/api/sessions", json={"player_name": "Ann"}).json()["session_id"]
    client.post(f"/api/sessions/{sid}/end")
    lb = client.get("/api/leaderboard").json()
    assert any(e["player_name"] == "Ann" for e in lb)
```

**Step 3: Run — expect fail.**

```bash
pytest tests/test_api.py -v
```
Expected: FAIL (`app.api.routes` empty / no router).

**Step 4: Implement** `app/api/routes.py`:

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.game.service import GameService
from app.schemas import (
    AnswerResult, LeaderboardEntry, SessionState, StartSessionRequest, SubmitAnswerRequest,
)

router = APIRouter()


def _svc(db: Session = Depends(get_db)) -> GameService:
    return GameService(db)


@router.post("/sessions", response_model=SessionState)
def start_session(body: StartSessionRequest, svc: GameService = Depends(_svc)):
    return svc.start_session(body.player_name)


@router.get("/sessions/{session_id}", response_model=SessionState)
def get_state(session_id: str, svc: GameService = Depends(_svc)):
    try:
        return svc.get_state(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


@router.post("/sessions/{session_id}/answer", response_model=AnswerResult)
def submit_answer(session_id: str, body: SubmitAnswerRequest, svc: GameService = Depends(_svc)):
    """Text answer endpoint — used by the frontend for manual testing and by tests.
    The voice bot calls GameService directly."""
    state = svc.get_state(session_id)
    if state.round_id is None:
        raise HTTPException(status_code=409, detail="no active round")
    return svc.submit_answer(session_id, state.round_id, body.transcript)


@router.post("/sessions/{session_id}/end", response_model=SessionState)
def end_session(session_id: str, svc: GameService = Depends(_svc)):
    try:
        return svc.end_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


@router.get("/leaderboard", response_model=list[LeaderboardEntry])
def leaderboard(limit: int = 10, svc: GameService = Depends(_svc)):
    return svc.leaderboard(limit)
```

**Step 5: Run — expect pass.**

```bash
pytest tests/test_api.py -v
```
Expected: PASS.

**Step 6: Full suite green + commit.**

```bash
pytest -v
git add app/main.py app/api/routes.py tests/test_api.py
git commit -m "feat: REST API for sessions, answers, end, leaderboard"
```

---

# Phase 7 — Pipecat voice pipeline

> **Verification note:** Pipecat's import paths and transport helpers change across 0.0.x releases. Before writing these files, open the installed package's `examples/` (especially the SmallWebRTC / p2p example) and mirror its exact imports and signaling flow. The code below is the intended shape; adjust import lines to match your installed version. Manual testing here (not unit tests) — a live WebRTC pipeline isn't unit-testable, and all game logic is already covered in Phases 3–6.

### Task 7.1: The custom game FrameProcessor

**Files:**
- Create: `app/voice/game_processor.py`

**Design / responsibilities:**
- Owns one session's voice turn loop.
- On start: `GameService.start_session`, then greet (LLM banter) + speak first sequence (deterministic).
- **Turn-taking (race-safe):** accumulate final `TranscriptionFrame` text during a turn, and finish the turn when we have *both* a "user stopped" signal *and* a transcript — whichever arrives last. This matters because Deepgram's final `TranscriptionFrame` often arrives **after** `UserStoppedSpeakingFrame` (the transport's VAD fires before the STT round-trip completes). Evaluating on `UserStoppedSpeakingFrame` alone would usually see an empty buffer and silently skip the turn.
- On user turn end: `GameService.submit_answer` (idempotent), then banter the verdict + either the next sequence or the game-over summary.
- **DB off the event loop:** all `GameService` calls (blocking SQLAlchemy + commit) run via `asyncio.to_thread(...)` so they never stall the audio pipeline on the hot path.
- **LLM vs. determinism:** the exact word sequence is spoken via a plain `TTSSpeakFrame` (bypasses the LLM, so the words can never be altered). Only host *personality* — greeting, reactions, game-over — is generated by Groq via `LLMMessagesFrame`. Validation and the spoken sequence stay 100% in code.
- **Interruptions:** on `StartInterruptionFrame`, drop the in-progress utterance and reset the current-turn state so a barge-in doesn't corrupt the answer or double-evaluate.

**Step 1:** Write it (verify frame/imports against installed Pipecat):

```python
# app/voice/game_processor.py
import asyncio

from pipecat.frames.frames import (
    Frame, LLMMessagesFrame, TTSSpeakFrame, TranscriptionFrame,
    UserStartedSpeakingFrame, UserStoppedSpeakingFrame, StartInterruptionFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

from app.db.database import SessionLocal
from app.game.service import GameService

# Host persona for the LLM. Kept to one short sentence for low latency, and told
# never to invent words — the actual sequence is spoken separately (see _speak).
HOST_SYSTEM = {
    "role": "system",
    "content": (
        "You are an upbeat, witty memory-game host. Reply in ONE short, "
        "energetic sentence. Never list, invent, or change any words yourself."
    ),
}


class MemoryGameProcessor(FrameProcessor):
    """Drives the memory game over voice. Game logic is delegated to GameService;
    this class only handles turn-taking, speaking, and interruption recovery.

    Two speech channels:
      * _speak()  -> TTSSpeakFrame  : deterministic, spoken verbatim (the sequence)
      * _banter() -> LLMMessagesFrame: Groq generates host personality lines
    """

    def __init__(self, player_name: str):
        super().__init__()
        self._player_name = player_name
        self._session_id: str | None = None
        self._round_id: str | None = None
        self._buffer: list[str] = []
        self._turn_active = False
        self._user_stopped = False

    # ---- speech helpers -------------------------------------------------
    async def _speak(self, text: str):
        """Deterministic TTS — bypasses the LLM so the exact words are preserved."""
        await self.push_frame(TTSSpeakFrame(text))

    async def _banter(self, instruction: str):
        """Ask Groq to voice a personality line based on game facts."""
        await self.push_frame(LLMMessagesFrame([HOST_SYSTEM, {"role": "user", "content": instruction}]))

    def _say_sequence(self, words: list[str]) -> str:
        return "Repeat after me: " + ", ".join(words) + "."

    # ---- blocking DB work, run off the event loop -----------------------
    def _start_session_sync(self):
        db = SessionLocal()
        try:
            svc = GameService(db)
            state = svc.start_session(self._player_name)
            seq = svc.get_current_sequence(state.session_id)
            return state, seq
        finally:
            db.close()

    def _submit_sync(self, transcript: str):
        db = SessionLocal()
        try:
            svc = GameService(db)
            result = svc.submit_answer(self._session_id, self._round_id, transcript)
            if result.status == "ENDED":
                return result, None, None
            state = svc.get_state(self._session_id)
            seq = svc.get_current_sequence(self._session_id)
            return result, state, seq
        finally:
            db.close()

    # ---- game flow ------------------------------------------------------
    async def _start_game(self):
        state, seq = await asyncio.to_thread(self._start_session_sync)
        self._session_id, self._round_id = state.session_id, state.round_id
        await self._banter(
            f"Greet the player named {self._player_name} and announce we're starting "
            f"round {state.current_round} of Memory Card."
        )
        await self._speak(self._say_sequence(seq))

    async def _handle_user_turn(self, transcript: str):
        result, state, seq = await asyncio.to_thread(self._submit_sync, transcript)
        if result.status == "ENDED":
            await self._banter(
                f"The player answered incorrectly. Console them and announce game over "
                f"with a final score of {result.total_score}."
            )
            await self._speak(f"The correct sequence was {', '.join(result.expected)}.")
            self._session_id = None
            return
        self._round_id = state.round_id
        await self._banter(
            f"Correct! The player earned {result.points_awarded} points for a total of "
            f"{result.total_score}. React with excitement and tell them round "
            f"{state.current_round} is next."
        )
        await self._speak(self._say_sequence(seq))

    # ---- turn detection -------------------------------------------------
    def _reset_turn(self):
        self._buffer.clear()
        self._turn_active = False
        self._user_stopped = False

    async def _finish_turn(self):
        """Fires once per turn, when we have BOTH a stop signal and a transcript."""
        if not self._turn_active:
            return
        transcript = " ".join(self._buffer).strip()
        self._reset_turn()
        if self._session_id and transcript:
            await self._handle_user_turn(transcript)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartInterruptionFrame):
            # barge-in: drop the in-progress utterance and current turn state
            self._reset_turn()

        elif isinstance(frame, UserStartedSpeakingFrame):
            self._buffer.clear()
            self._turn_active = True
            self._user_stopped = False

        elif isinstance(frame, TranscriptionFrame) and self._turn_active:
            self._buffer.append(frame.text)
            # transcript may arrive AFTER the stop signal — finish here if so
            if self._user_stopped:
                await self._finish_turn()

        elif isinstance(frame, UserStoppedSpeakingFrame) and self._turn_active:
            self._user_stopped = True
            # if the final transcript already arrived, finish now;
            # otherwise wait for it in the TranscriptionFrame branch above
            if self._buffer:
                await self._finish_turn()

        await self.push_frame(frame, direction)
```

**Step 2:** Commit.

```bash
git add app/voice/game_processor.py
git commit -m "feat: memory game frame processor (turn-taking + interruptions)"
```

### Task 7.2: Pipeline assembly (bot.py)

**Files:**
- Create: `app/voice/bot.py`

**Step 1:** Write it (verify service/transport imports against installed Pipecat):

```python
# app/voice/bot.py
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.transports.base_transport import TransportParams

from app.config import settings
from app.voice.game_processor import MemoryGameProcessor


async def run_bot(transport, player_name: str = "Player"):
    stt = DeepgramSTTService(api_key=settings.deepgram_api_key)
    tts = DeepgramTTSService(api_key=settings.deepgram_api_key, voice="aura-asteria-en")
    llm = GroqLLMService(api_key=settings.groq_api_key, model=settings.groq_model)
    game = MemoryGameProcessor(player_name=player_name)

    # Order matters: the game processor emits either an LLMMessagesFrame (host
    # banter -> llm -> tts) or a TTSSpeakFrame (exact sequence). Placing the LLM
    # AFTER the game processor lets TTSSpeakFrames pass through the LLM untouched
    # while LLMMessagesFrames get turned into speech. Validation stays in code.
    pipeline = Pipeline([
        transport.input(),
        stt,
        game,
        llm,
        tts,
        transport.output(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),   # enable barge-in
    )

    # kick off the game when the client connects
    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):
        await game._start_game()

    runner = PipelineRunner()
    await runner.run(task)


# TransportParams builder used by the webrtc route
def default_transport_params() -> TransportParams:
    return TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(),   # detects end-of-turn for turn-taking
    )
```

> **Why the LLM is safe here:** Groq only generates host *personality* lines from game facts (correct/incorrect, score, round) via `LLMMessagesFrame`. The actual word sequence is spoken with a plain `TTSSpeakFrame` that passes through the LLM untouched, so the LLM can never alter or invent the words. Sequence generation, comparison, and scoring remain 100% in `engine.py` — satisfying "validation must not depend on an LLM." If you want to first prove the game end-to-end without LLM latency, you can temporarily drop `llm` from the pipeline and swap `_banter(...)` calls for `_speak(...)`, then add it back.

**Step 2:** Commit.

```bash
git add app/voice/bot.py
git commit -m "feat: pipecat pipeline assembly (deepgram stt/tts + vad)"
```

### Task 7.3: SmallWebRTC signaling route + mount static

**Files:**
- Create: `app/voice/webrtc.py`
- Modify: `app/main.py`

> ⚠️ **This is the highest-risk file in the plan — treat the code below as pseudocode.** The SmallWebRTC connection class, its `initialize`/answer methods (often async), the transport constructor, and the connect event name all vary by Pipecat release. **Before writing this file, open the installed version's SmallWebRTC / p2p example and copy its offer handler verbatim**, then adapt only the two project-specific bits: (a) reading `player_name` from the request body, and (b) starting `run_bot(transport, player_name)`. Do not hand-write the SDP handshake from memory.
>
> Find the example with:
> ```bash
> python -c "import pipecat, os; print(os.path.dirname(pipecat.__file__))"
> # then browse the sibling examples/ dir, or see docs.pipecat.ai for SmallWebRTC
> ```

**Step 1:** `app/voice/webrtc.py` — mirror the installed Pipecat SmallWebRTC example's offer/answer handler (illustrative shape only):

```python
# app/voice/webrtc.py
# PSEUDOCODE — replace the handshake with the installed pipecat example's exact code.
import asyncio

from fastapi import APIRouter, Request
from pipecat.transports.network.small_webrtc import SmallWebRTCTransport
from pipecat.transports.network.webrtc_connection import SmallWebRTCConnection

from app.voice.bot import default_transport_params, run_bot

router = APIRouter()


@router.post("/offer")
async def offer(request: Request):
    body = await request.json()
    player_name = body.get("player_name", "Player")

    # --- BEGIN: replace with the exact handshake from your pipecat version ---
    connection = SmallWebRTCConnection()
    await connection.initialize(sdp=body["sdp"], type=body["type"])
    transport = SmallWebRTCTransport(connection=connection, params=default_transport_params())
    answer = connection.get_answer()   # may be async in your version: `await ...`
    # --- END ---

    # run the bot for this connection in the background
    asyncio.create_task(run_bot(transport, player_name))
    return answer   # {"sdp": ..., "type": "answer"}
```

**Step 2:** Extend `app/main.py` to mount the webrtc router + static files:

```python
# app/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db.database import Base, engine
from app.api.routes import router as api_router
from app.voice.webrtc import router as webrtc_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # See Task 6.1 note: table creation stays in lifespan, not at import time,
    # so importing app.main never connects to Postgres (keeps tests DB-free).
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Memory Card Voice Bot", lifespan=lifespan)
app.include_router(api_router, prefix="/api")
app.include_router(webrtc_router, prefix="/rtc")
app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

**Step 3:** Sanity boot (Postgres must be up, keys set):

```bash
uvicorn app.main:app --port 8000
```
Expected: server starts, no import errors. (Ctrl-C to stop.)

**Step 4:** Commit.

```bash
git add app/voice/webrtc.py app/main.py
git commit -m "feat: smallwebrtc signaling route and static mount"
```

---

# Phase 8 — Minimal frontend

### Task 8.1: index.html + style.css

**Files:**
- Create: `static/index.html`, `static/style.css`

**Step 1:** `static/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Memory Card Voice Bot</title>
  <link rel="stylesheet" href="/style.css" />
</head>
<body>
  <main>
    <h1>🧠 Memory Card Voice Bot</h1>
    <div id="setup">
      <input id="name" placeholder="Your name" value="Player" />
      <button id="start">Start voice game</button>
    </div>
    <section id="game" hidden>
      <div class="stat">Status: <span id="status">—</span></div>
      <div class="stat">Round: <span id="round">—</span></div>
      <div class="stat">Score: <span id="score">0</span></div>
      <div class="stat">Sequence length: <span id="len">—</span></div>
      <button id="end">End game</button>
    </section>
    <h2>🏆 Leaderboard</h2>
    <ol id="leaderboard"></ol>
  </main>
  <script src="/app.js"></script>
</body>
</html>
```

**Step 2:** `static/style.css`:

```css
body { font-family: system-ui, sans-serif; max-width: 560px; margin: 40px auto; padding: 0 16px; }
h1 { font-size: 1.5rem; }
.stat { font-size: 1.1rem; margin: 6px 0; }
button { padding: 8px 14px; margin: 6px 0; cursor: pointer; }
input { padding: 8px; }
#leaderboard li { margin: 4px 0; }
```

**Step 3:** Commit.

```bash
git add static/index.html static/style.css
git commit -m "feat: minimal frontend markup and styles"
```

### Task 8.2: app.js — WebRTC connect + state polling

**Files:**
- Create: `static/app.js`

**Step 1:** Write it (adjust the offer flow to match your Pipecat SmallWebRTC example if needed):

```javascript
// static/app.js
let sessionId = null;
let pc = null;
let pollTimer = null;

async function startGame() {
  const playerName = document.getElementById("name").value || "Player";

  // 1) create a session via the REST API (also warms cache + DB)
  const res = await fetch("/api/sessions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ player_name: playerName }),
  });
  const state = await res.json();
  sessionId = state.session_id;
  renderState(state);
  document.getElementById("game").hidden = false;

  // 2) open mic + WebRTC to the bot
  pc = new RTCPeerConnection();
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  stream.getTracks().forEach((t) => pc.addTrack(t, stream));
  pc.ontrack = (e) => {
    const audio = new Audio();
    audio.srcObject = e.streams[0];
    audio.play();
  };

  const offer = await pc.createOffer({ offerToReceiveAudio: true });
  await pc.setLocalDescription(offer);

  const rtcRes = await fetch("/rtc/offer", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sdp: offer.sdp, type: offer.type, player_name: playerName }),
  });
  const answer = await rtcRes.json();
  await pc.setRemoteDescription(answer);

  // 3) poll game state so the UI reflects rounds/score as they change
  pollTimer = setInterval(refreshState, 1500);
  refreshLeaderboard();
}

async function refreshState() {
  if (!sessionId) return;
  const res = await fetch(`/api/sessions/${sessionId}`);
  if (res.ok) {
    const state = await res.json();
    renderState(state);
    if (state.status === "ENDED") stopPolling();
  }
}

function renderState(s) {
  document.getElementById("status").textContent = s.status;
  document.getElementById("round").textContent = s.current_round;
  document.getElementById("score").textContent = s.score;
  document.getElementById("len").textContent = s.sequence_length ?? "—";
}

async function endGame() {
  if (!sessionId) return;
  await fetch(`/api/sessions/${sessionId}/end`, { method: "POST" });
  stopPolling();
  if (pc) pc.close();
  await refreshState();
  await refreshLeaderboard();
}

function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

async function refreshLeaderboard() {
  const res = await fetch("/api/leaderboard");
  const rows = await res.json();
  const ol = document.getElementById("leaderboard");
  ol.innerHTML = "";
  rows.forEach((r) => {
    const li = document.createElement("li");
    li.textContent = `${r.player_name} — ${r.score}`;
    ol.appendChild(li);
  });
}

document.getElementById("start").addEventListener("click", startGame);
document.getElementById("end").addEventListener("click", endGame);
```

**Step 2:** Commit.

```bash
git add static/app.js
git commit -m "feat: frontend webrtc connect and state polling"
```

---

# Phase 9 — Manual end-to-end + README

### Task 9.1: Full manual smoke test

**Step 1:** Ensure Postgres up and `.env` has real keys:

```bash
docker compose up -d
cp .env.example .env   # then edit DEEPGRAM_API_KEY and GROQ_API_KEY
```

**Step 2:** Run the app:

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

**Step 3:** Open `http://localhost:8000`, click **Start voice game**, allow mic.

**Verify checklist:**
- [ ] Bot greets and speaks the round-1 sequence (3 words).
- [ ] Repeat correctly → bot says "Correct", score/round update in UI.
- [ ] Sequence grows each round (4, 5, …).
- [ ] Repeat incorrectly → bot ends game with final score; UI shows ENDED.
- [ ] Leaderboard shows the finished session.
- [ ] **Interrupt the bot mid-sentence** → it stops cleanly and doesn't mis-score (for the video).
- [ ] Re-submitting via `POST /api/sessions/{id}/answer` for an answered round returns the same result (no double score).

**Step 4:** Verify persistence directly:

```bash
docker compose exec db psql -U voiceflash -d voiceflash -c \
  "select player_name, score, status from game_sessions order by created_at desc limit 5;"
```

### Task 9.2: README

**Files:**
- Create: `README.md`

**Step 1:** Write setup + architecture + "where each requirement is met" (reuse the traceability table), run steps, API reference, and a short "how caching/idempotency work" section. Include the run commands from *How you'll run it* above.

**Step 2:** Commit.

```bash
git add README.md
git commit -m "docs: readme with setup, architecture, api reference"
```

### Task 9.3: Final green + tag

**Step 1:** Full suite:

```bash
pytest -v
```
Expected: all PASS.

**Step 2:** Commit any cleanup and tag a checkpoint:

```bash
git add -A && git commit -m "chore: polish" || true
git tag v1.0-mvp
```

---

# Phase 10 — Video walkthrough checklist (deliverable #2)

Record 10–15 min covering:
1. **Demo:** start session, play multiple rounds, show a correct and an incorrect answer, show final score + leaderboard.
2. **Interruption:** barge in while the bot speaks; show clean recovery.
3. **Code walkthrough:** `bot.py` (Pipecat pipeline), `game_processor.py` (custom frame processor + turn-taking + interruption), `models.py` (DB), `routes.py` (APIs), `store.py` + `service.py` (caching + idempotent scoring), `app.js` (frontend↔backend).

---

## Risks / things to watch (honest list)

- **Pipecat API drift:** Phase 7 imports/transport helpers may differ from your installed 0.0.x — verify against the package's `examples/`. This is the single biggest execution risk; do Phase 7 with the docs open.
- **STT accuracy on rare words:** Deepgram may mishear unusual words; the seeded pool in `words.py` is chosen to be phonetically distinct. If accuracy is poor, trim the pool or add a small homophone map in `engine.normalize`.
- **Turn-taking edge cases:** the processor finishes a turn on whichever arrives last (stop signal or final transcript), which fixes the common empty-buffer race — but if the user pauses *mid-sequence*, VAD may still end the turn early. Tune Silero VAD stop-seconds in `default_transport_params()` if needed. If a final transcript never arrives after a stop (pure silence), the turn simply waits; add a timeout only if this bites in testing.
- **`Base.metadata.create_all`** runs in the FastAPI lifespan handler (not at import) instead of migrations — fine for the assignment; mention in README. This is also what keeps `pytest` from needing Postgres.
```