# Memory Card Voice Bot

A voice-based Memory Card game: the bot speaks a growing sequence of words over a live
WebRTC voice call, the player repeats it back, and the backend deterministically validates
the answer, scores it, persists everything, and progresses the game.

Built with **Pipecat** (voice pipeline), **FastAPI** (REST + WebRTC signaling + static UI),
**PostgreSQL** (persistence), and **Redis** (active session state + leaderboard cache,
shared across processes so multiple users can play at once — see
[Concurrency & multi-user notes](#concurrency--multi-user-notes)).

---

## Architecture

One Python process does three jobs: serves REST APIs for sessions/state/scores/leaderboard,
hosts the Pipecat voice pipeline over browser WebRTC, and serves a minimal web UI.

The game **brain lives in one place** — a pure `app/game/engine.py` (sequence generation,
transcript normalization, comparison, scoring) wrapped by `app/game/service.py`
(`GameService`), which adds Postgres persistence and the Redis cache. Both the REST
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
                                        │ GameService │  │    Redis     │  active session state
                                        │  + engine   │  │  (shared)    │  + leaderboard
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
│  └─ store.py              # Redis-backed cache (active session, leaderboard)
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
- Docker (for Postgres and Redis)
- Deepgram API key (STT + TTS) — free tier available
- Groq API key (LLM host banter) — free tier available

### First-time setup

1. **Start Postgres and Redis:**

   ```bash
   docker compose up -d
   docker compose ps      # wait until "db" and "redis" both show (healthy)
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
docker compose up -d                       # if Postgres/Redis aren't already running
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

> **Note on the Postgres/Redis ports:** `docker-compose.yml` maps Postgres to **host port
> 5433** (not 5432) and Redis to **host port 6380** (not 6379). This project was developed
> on a machine that already had native Postgres and Redis instances bound to their default
> ports, and macOS silently routes `localhost:<port>` to whichever process claimed that
> specific address first — which caused connections intended for the Docker containers to
> hit the wrong service entirely (a `role "voiceflash" does not exist` error for Postgres).
> Remapping sidesteps the conflict; if the default ports are free on your machine you don't
> need to change anything. `.env.example` and `app/config.py`'s defaults already point at
> `5433`/`6380`.

### Troubleshooting

- **`uvicorn: command not found`** — the virtualenv isn't active; run
  `source .venv/bin/activate` first.
- **`OperationalError` / `role "voiceflash" does not exist`** — something else is already
  listening on the Postgres port. Check with `lsof -nP -iTCP:5433 -sTCP:LISTEN` (or `:5432`
  if you changed it back) and make sure it's the `voiceflash-db-1` container.
- **Cache doesn't seem to update, or `redis.exceptions.ConnectionError`** — Redis isn't
  reachable. Check with `lsof -nP -iTCP:6380 -sTCP:LISTEN` and make sure it's the
  `voiceflash-redis-1` container (or run `docker compose ps`).
- **Server already running** — check `lsof -nP -iTCP:8000 -sTCP:LISTEN` before starting a
  second instance; only one process can bind port 8000 at a time.

Run the tests (no Postgres, Redis, or API keys required — the suite uses an isolated
in-memory SQLite database and an in-memory Redis stand-in, `fakeredis`):

```bash
pytest -v
```

---

## How to play

1. Open **http://localhost:8000**.
2. Type your name (or keep "Player") and click **🎙️ Start voice game**.
3. Your browser will ask for **microphone access** — allow it. That's the bot listening
   for your spoken answers, not a recording that gets saved anywhere.
4. Wait a moment while it connects (you'll see a spinner), then the host greets you and
   speaks a short sequence of words.
5. **Repeat the sequence back out loud, in order**, then stop talking — the bot waits
   until you're actually done before it judges you (see [Turn-taking](#voice-pipeline-notes)).
6. Get it right → you score points and the *next* sequence is one word longer.
   Get it wrong → the game ends and your final score is shown.
7. Click **🏳️ End game** any time to stop early and lock in your current score.
8. Try **talking over the bot** while it's mid-sentence — it should stop cleanly and
   let you answer instead of ignoring you or getting confused. That's the
   interruption-handling requirement in action.

### What a round actually sounds like

```
🤖 Bot:  "Hey Alex! Let's kick off round 1 of Memory Card."
🤖 Bot:  "Repeat after me: apple, tiger, river."
🗣️ You:  "Apple, tiger, river."
🤖 Bot:  "Correct! That's 30 points. Nice start — round 2 coming up!"
🤖 Bot:  "Repeat after me: guitar, planet, candle, rocket."
🗣️ You:  "Guitar, planet... rocket?"                          ← missed a word
🤖 Bot:  "Ohh, so close! Game over — you finished with 30 points."
```

Only the sequence lines ("Repeat after me: …") are ever spoken word-for-word by a fixed
script — those are never touched by the LLM. Everything else the bot says (greetings,
reactions, the game-over line) is generated on the fly by Groq so it doesn't feel scripted,
but it never decides whether you were *right* — that judgment always comes from
`engine.evaluate()` comparing your transcript to the exact sequence in the database.

### Reading the screen while you play

| You see | It means |
|---|---|
| 🟢 pulsing **● ACTIVE** badge | Your session is live and the bot is expecting an answer |
| 🔴 **● ENDED** badge | Game over — either you missed a sequence or clicked "End game" |
| Row of little gradient cards | How many words are in the *current* sequence — grows every round |
| Score number "bumping" bigger | You just scored — confetti means you got it right |
| Pulsing 🎤 | The bot is actively listening for your voice |
| 🏆 Leaderboard with medals | Top finished sessions across everyone who's played, ranked by score |

If you want to sanity-check the game without talking to it, the same `POST
/api/sessions/{id}/answer` endpoint the tests use also works from a REST client (see
[API reference](#api-reference)) — handy for demoing the "no double-scoring" behavior by
submitting the same round twice and seeing the score not change the second time.

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

`app/cache/store.py` is backed by **Redis** (`redis-py`), with two TTL'd keys per concern:

- **Active session state** (30 min TTL) — read on every voice turn and every
  `GET /api/sessions/{id}` call. `GameService.get_state` is cache-first and only falls back
  to Postgres on a cache miss, re-warming the cache afterward.
- **Leaderboard** (60s TTL) — invalidated whenever a session ends (score changes), so it
  never serves stale rankings for longer than a minute past a state change.

Redis rather than an in-process cache specifically so that **multiple server
processes/instances share the same view** of active sessions and the leaderboard — see
[Concurrency & multi-user notes](#concurrency--multi-user-notes) below. Tests never need a
real Redis: `tests/conftest.py` swaps in `fakeredis` (an in-memory stand-in with the same
wire protocol) via an autouse fixture.

## Avoiding double-scoring

`GameService.submit_answer` checks whether the target `Round` already has a stored
`Response` before evaluating; if so, it returns the previously computed `AnswerResult`
unchanged instead of re-running `engine.evaluate()`. This is backed by a database-level
`UniqueConstraint` on `Response.round_id`, so even a race between two concurrent submissions
for the same round can't produce two scored responses — see the next section for how the
*second* (losing) request is handled without crashing.

---

## Concurrency & multi-user notes

This app is expected to have multiple people playing simultaneously — each in their own
session, with their own voice call. Two concerns worth calling out explicitly:

**1. A same-round race condition (fixed in code, not just by the DB constraint).**
`GameService.submit_answer` originally read the `Round`, checked whether it was already
answered, and only *then* wrote — a classic check-then-act race. If the same round were
submitted twice at almost the same instant (e.g. a flaky client double-firing, or two voice
turns overlapping), both requests could pass the "already answered?" check before either had
committed, and the second `commit()` (or, in the correct-answer path, an earlier `flush()`)
would hit the database's `UniqueConstraint` on `Response.round_id` and raise an unhandled
`IntegrityError` — a 500 instead of a clean result. `submit_answer` now wraps that write in
`try/except IntegrityError`: on conflict, it rolls back its own attempt, re-fetches the round,
and returns the *winning* request's stored result — the same contract as the ordinary
idempotency check above, just reached via a different path (a stale read, rather than a
sequential resubmission). Covered by
`tests/test_service.py::test_submit_answer_recovers_from_concurrent_insert_conflict`, which
deliberately constructs that race window and asserts the call returns cleanly instead of
raising.

**2. Why the cache had to move to Redis.** The original active-session/leaderboard cache
was an in-process `cachetools.TTLCache` — fine for one Uvicorn worker, but if you scale out
to multiple worker processes or multiple machines behind a load balancer, each process would
hold its *own* cache. A session started on process A would warm process A's cache only;
a request that happened to land on process B would see a cache miss (harmless, just a
Postgres round-trip) — but worse, if B then wrote its own stale copy back, A and B's caches
could disagree about the same session's state. Redis fixes this by being the *one* shared
cache every process reads and writes, so horizontal scaling no longer risks cache
inconsistency. `GameService`'s code didn't change at all for this — `app/cache/store.py`
keeps the exact same function signatures, just backed by Redis instead of an in-process dict.

**What this doesn't (yet) solve:** a live WebRTC voice call is inherently pinned to whichever
process accepted its `SmallWebRTCConnection` — you can't move an in-progress voice call
between processes. Scaling voice traffic horizontally means routing new connections across
multiple instances (e.g. round-robin at the load balancer, since each call is independent and
short-lived), not sharing one call's state across processes. The REST API and leaderboard,
by contrast, are now fully stateless across instances thanks to Redis + Postgres, so they
scale behind a plain load balancer with no special routing.

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
| Caching ≥1 meaningful flow | `app/cache/store.py` (active session + leaderboard, Redis-backed) |
| Avoid double-scoring, incl. concurrent requests | idempotent `submit_answer` + DB unique constraint on `round_id` + `IntegrityError` recovery (see [Concurrency & multi-user notes](#concurrency--multi-user-notes)) |
| Validation in code, not LLM | `app/game/engine.py` |
| Word list hardcoded/seeded | `app/game/words.py` |
