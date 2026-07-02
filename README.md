# Memory Card Voice Bot

A voice-based Memory Card game: the bot speaks a growing sequence of words over a live
WebRTC voice call, the player repeats it back, and the backend deterministically validates
the answer, scores it, persists everything, and progresses the game.

Built with **Pipecat** (voice pipeline), **FastAPI** (REST + WebRTC signaling + static UI),
**PostgreSQL** (persistence), and an **in-memory TTL cache** (active session state +
leaderboard).

---

## Architecture

One Python process does three jobs: serves REST APIs for sessions/state/scores/leaderboard,
hosts the Pipecat voice pipeline over browser WebRTC, and serves a minimal web UI.

The game **brain lives in one place** — a pure `app/game/engine.py` (sequence generation,
transcript normalization, comparison, scoring) wrapped by `app/game/service.py`
(`GameService`), which adds Postgres persistence and the in-memory cache. Both the REST
routes (`app/api/routes.py`) and the voice pipeline's custom frame processor
(`app/voice/game_processor.py`) call the *same* `GameService` — a single source of truth,
no duplicated logic. Answer validation is 100% deterministic code; it never depends on the
LLM. The LLM (Groq) is used only to phrase engaging game-host banter (greetings, reactions,
game-over lines) — the actual word sequence is always spoken verbatim via a direct TTS frame
that bypasses the LLM entirely.

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

### Directory layout

```
app/
├─ main.py                  # FastAPI app: mounts API + webrtc + static
├─ config.py                # env-driven settings
├─ schemas.py                # Pydantic request/response DTOs
├─ db/
│  ├─ database.py           # engine + SessionLocal + Base
│  └─ models.py              # GameSession, Round, Response
├─ cache/
│  └─ store.py              # TTLCache wrappers (active session, leaderboard)
├─ game/
│  ├─ words.py               # seeded word list
│  ├─ engine.py              # PURE logic (no DB, no cache)
│  └─ service.py             # GameService = engine + persistence + cache
├─ api/
│  └─ routes.py              # REST endpoints
└─ voice/
   ├─ bot.py                 # Pipecat pipeline assembly
   ├─ game_processor.py      # custom FrameProcessor that drives the game
   └─ webrtc.py               # SmallWebRTC signaling route
static/                      # minimal HTML/JS/CSS frontend
tests/                       # pytest suite (engine, cache, service, API)
```

---

## Game rules (deterministic — implemented in `engine.py`)

- A **sequence** is an ordered list of simple words drawn from a seeded pool (`game/words.py`).
- **Difficulty ramp:** round *n* sequence length = `min(2 + n, MAX_LEN)` → round 1 = 3 words,
  round 2 = 4, … capped at `MAX_LEN` (default 8, via `MAX_SEQUENCE_LENGTH`).
- The bot speaks the sequence, the user repeats it, and `engine.normalize()` lowercases,
  strips punctuation, and drops filler words (`um`, `uh`, `the`, …) from the transcript
  before comparing it token-by-token, in order, against the expected sequence.
- **Correct:** award `10 × sequence_length` points, advance to the next round.
- **Incorrect:** game over; final score persisted (classic single-life memory game).
- **Idempotent scoring:** each round is answered exactly once. A repeat submission for an
  already-answered round returns the stored result instead of re-scoring — enforced both in
  `GameService.submit_answer` (checks `Round.status`/`Round.response`) and at the DB level via
  a unique constraint on `Response.round_id`.

---

## Setup

### Prerequisites
- Python 3.11+ (Pipecat requires 3.10+)
- Docker (for Postgres)
- Deepgram API key (STT + TTS) — free tier available
- Groq API key (LLM host banter) — free tier available

### First-time setup

1. **Start Postgres:**

   ```bash
   docker compose up -d
   docker compose ps      # wait until the "db" service shows (healthy)
   ```

2. **Create the virtualenv and install dependencies:**

   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure environment variables:**

   ```bash
   cp .env.example .env
   ```

   Then edit `.env` and fill in `DEEPGRAM_API_KEY` and `GROQ_API_KEY`. `.env` is
   git-ignored, so your keys never get committed.

4. **Run the server:**

   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

   Open **http://localhost:8000** in your browser, allow microphone access, and click
   "Start voice game".

### Starting the server again later

Once the one-time setup above is done, restarting only needs:

```bash
docker compose up -d                       # if Postgres isn't already running
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

> **Note on the Postgres port:** `docker-compose.yml` maps the container's Postgres to
> **host port 5433**, not the default 5432. This project was developed on a machine that
> already had a native Postgres instance bound to `5432`, and macOS silently routes
> `localhost:5432` to whichever process claimed that specific address first — which caused
> connections intended for the Docker container to hit the wrong database entirely (a
> `role "voiceflash" does not exist` error). Mapping to `5433` sidesteps the conflict; if
> port 5433 is free on your machine you don't need to change anything. `.env.example` and
> `app/config.py`'s default both already point at `5433`.

### Troubleshooting

- **`uvicorn: command not found`** — the virtualenv isn't active; run
  `source .venv/bin/activate` first.
- **`OperationalError` / `role "voiceflash" does not exist`** — something else is already
  listening on the Postgres port. Check with `lsof -nP -iTCP:5433 -sTCP:LISTEN` (or `:5432`
  if you changed it back) and make sure it's the `voiceflash-db-1` container.
- **Server already running** — check `lsof -nP -iTCP:8000 -sTCP:LISTEN` before starting a
  second instance; only one process can bind port 8000 at a time.

Run the tests (no Postgres or API keys required — the suite uses an isolated in-memory
SQLite database):

```bash
pytest -v
```

---

## API reference

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/sessions` | Start a new session. Body: `{"player_name": "..."}`. Returns `SessionState`. |
| `GET` | `/api/sessions/{session_id}` | Fetch current game state (status, round, score). Never exposes the word sequence, only its length. |
| `POST` | `/api/sessions/{session_id}/answer` | Submit a text transcript for the current round. Used by tests/manual testing; the voice bot calls `GameService` directly instead. |
| `POST` | `/api/sessions/{session_id}/end` | End a session early. |
| `GET` | `/api/leaderboard?limit=10` | Top sessions by score (cached). |
| `POST` | `/rtc/offer` | WebRTC SDP offer/answer signaling to start a voice session. Body: `{"sdp", "type", "player_name"}`. |

---

## Caching

`app/cache/store.py` uses two `cachetools.TTLCache` instances:

- **Active session state** (30 min TTL, up to 1000 sessions) — read on every voice turn and
  every `GET /api/sessions/{id}` call. `GameService.get_state` is cache-first and only falls
  back to Postgres on a cache miss, re-warming the cache afterward.
- **Leaderboard** (60s TTL) — invalidated whenever a session ends (score changes), so it
  never serves stale rankings for longer than a minute past a state change.

## Avoiding double-scoring

`GameService.submit_answer` checks whether the target `Round` already has a stored
`Response` before evaluating; if so, it returns the previously computed `AnswerResult`
unchanged instead of re-running `engine.evaluate()`. This is backed by a database-level
`UniqueConstraint` on `Response.round_id`, so even a race between two concurrent submissions
for the same round can't produce two scored responses.

---

## Voice pipeline notes

- **Turn-taking:** Deepgram's final transcript frequently arrives *after* the transport's
  VAD emits `UserStoppedSpeakingFrame`. `MemoryGameProcessor` finishes a turn on whichever of
  the two arrives last, so it never evaluates against an empty buffer.
- **Interruptions:** on `StartInterruptionFrame` (barge-in), the processor drops the
  in-progress turn state so an interrupted utterance is never partially scored.
- **Validation vs. the LLM:** the exact word sequence is always spoken via a plain
  `TTSSpeakFrame`, which passes through the LLM stage of the pipeline untouched. Only host
  *personality* (greetings, reactions, game-over lines) goes through Groq via
  `LLMMessagesFrame`. Sequence generation, transcript comparison, and scoring are 100%
  deterministic Python in `engine.py`.

---

## Requirements traceability

| Requirement | Where |
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
