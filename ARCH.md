# Architecture

This document explains the system design of the Memory Card Voice Bot: what each
piece is, why it was chosen, and specifically how the system behaves under
multiple simultaneous users, partial failures, and concurrent writes. For setup
instructions and gameplay, see [README.md](README.md).

---

## 1. Goal and constraints

A voice-based memory game: the bot speaks a growing word sequence over a live
WebRTC call, the player repeats it back, and the backend deterministically
judges the answer, scores it, persists it, and advances the game. The two
constraints that shaped every decision below:

- **Validation must never depend on an LLM.** The word sequence, the
  comparison, and the score are 100% deterministic code.
- **Multiple people can play at the same time**, each with their own session
  and their own voice call, without interfering with each other.

---

## 2. Tech stack and why

| Piece | Choice | Why |
|---|---|---|
| Voice pipeline | **Pipecat** (`pipecat-ai==0.0.108`) | Required by the assignment; gives a composable frame-processing pipeline (STT → custom logic → LLM → TTS) instead of hand-rolling audio plumbing. |
| STT + TTS | **Deepgram** | Real-time streaming STT with low latency; free-tier friendly. Used for both directions so there's one vendor relationship for voice I/O. |
| LLM (banter only) | **Groq** (`llama-3.1-8b-instant`) | Fast inference keeps host chatter snappy; deliberately **never** used for judging correctness — see §5. |
| Transport | **SmallWebRTC** (Pipecat's aiortc-based transport) | Runs entirely in-process (no separate media server like Daily/LiveKit needed), which keeps the whole system to one deployable unit for this assignment's scope. |
| API framework | **FastAPI** | Async-native, plays well with Pipecat's asyncio pipeline, gives typed request/response models via Pydantic for free. |
| Database | **PostgreSQL** | Relational integrity (foreign keys, unique constraints) is what actually prevents double-scoring — see §6. Not a NoSQL fit; the data is inherently relational (session → rounds → responses). |
| Cache | **Redis** | Shared, out-of-process cache so the active-session/leaderboard cache is consistent across multiple server processes — see §4. |
| Frontend | Vanilla HTML/CSS/JS | No build step, no framework overhead, for a UI this small (name entry, WebRTC handshake, state polling, leaderboard). |
| Tests | **pytest** + SQLite (`db` fixture) + **fakeredis** | The full suite runs with zero external services — no Postgres, no Redis, no API keys — so CI/local iteration is fast and hermetic. |

---

## 3. Component architecture

```
                 ┌──────────────────────── Browser (static/) ────────────────────────┐
                 │  mic/audio  ──WebRTC──►                     ◄── polls GET /api/... ─│
                 └───────────────┬───────────────────────────────────┬───────────────┘
                                 │ audio                              │ JSON
                    ┌────────────▼─────────────┐        ┌─────────────▼──────────────┐
                    │   Pipecat pipeline         │        │     FastAPI REST routes    │
                    │  input → STT → GameProc    │        │  /sessions /state /end     │
                    │  → LLM → TTS → output      │        │  /answer /leaderboard      │
                    │  (Deepgram)  (Groq banter) │        └─────────────┬──────────────┘
                    └────────────┬──────────────┘                      │
                                 │        both call the SAME service    │
                                 └────────────────┬─────────┬──────────┘
                                                   ▼         ▼
                                          ┌─────────────┐ ┌────────────┐
                                          │ GameService │ │   Redis    │  active session
                                          │  + engine   │ │  (shared)  │  state + leaderboard
                                          └──────┬──────┘ └────────────┘  cache, TTL'd
                                                  ▼
                                          ┌─────────────┐
                                          │ PostgreSQL  │  sessions, rounds,
                                          │             │  responses, scores
                                          └─────────────┘
```

### 3.1 The single source of truth: `engine.py` → `service.py`

The game's "brain" lives in exactly one place:

- **`app/game/engine.py`** — pure functions, no I/O: sequence generation,
  difficulty ramp, transcript normalization, evaluation, scoring. Fully unit
  tested in isolation (`tests/test_engine.py`).
- **`app/game/service.py`** (`GameService`) — wraps `engine.py` with Postgres
  persistence and the Redis cache. Owns every state transition: starting a
  session, reading state, submitting an answer, ending a session, computing
  the leaderboard.

**Both entry points — the REST API (`app/api/routes.py`) and the voice
pipeline's custom processor (`app/voice/game_processor.py`) — call the same
`GameService`.** There is no duplicated game logic between "the API version"
and "the voice version." This is what makes it possible to reason about
correctness once and get it for both interfaces.

### 3.2 Voice pipeline internals

`app/voice/bot.py` assembles a linear Pipecat pipeline:

```
transport.input() → STT (Deepgram) → MemoryGameProcessor → LLM (Groq) → TTS (Deepgram) → transport.output()
```

`MemoryGameProcessor` (`app/voice/game_processor.py`) is the custom
`FrameProcessor` that drives the game over voice. It has two speech channels,
kept deliberately separate:

- **`_speak()`** → `TTSSpeakFrame` — the exact word sequence, spoken verbatim,
  bypassing the LLM entirely. This is the mechanism that guarantees the LLM
  can never alter the words being tested.
- **`_banter()`** → `LLMMessagesFrame` — host personality (greetings,
  reactions, game-over lines), generated by Groq from game *facts* the
  processor computed, never from raw judgment.

Turn-taking and barge-in are both handled in this processor rather than
relying on Pipecat defaults, because this pipeline intentionally has no
`turn_analyzer` or `LLMUserAggregator` (those pull in a much larger,
opinionated conversation-management layer this project doesn't need). Two
consequences of that choice, both handled explicitly:

- **Turn-taking**: Deepgram's final transcript frequently arrives *after* the
  transport's VAD emits `VADUserStoppedSpeakingFrame`. The processor finishes
  a turn only once **both** signals have arrived, whichever comes last —
  otherwise it would evaluate an empty buffer.
- **Barge-in**: without a `turn_analyzer`, Pipecat's transport never emits
  `StartInterruptionFrame` on its own in this configuration. The processor
  tracks `BotStartedSpeakingFrame`/`BotStoppedSpeakingFrame` itself and calls
  the stable public `broadcast_interruption()` method when VAD detects the
  user talking while the bot is mid-speech — that's what actually halts
  in-flight TTS generation and audio output.

---

## 4. Scalability

The design goal was: **the REST API and leaderboard scale horizontally with
zero special handling; voice calls scale by routing, not by shared state.**

- **Stateless REST layer.** Every REST request re-derives what it needs from
  Redis (fast path) or Postgres (cold path) — no server-local session state.
  Any request can be served by any process/instance.
- **Redis as the shared cache**, not an in-process one. The original
  implementation used an in-process `cachetools.TTLCache`; it was replaced
  specifically because a single process's cache doesn't help — or worse,
  actively lies — once you run more than one Uvicorn worker or scale to
  multiple machines behind a load balancer. Two processes with their own
  in-memory caches would disagree about the same session's state. Redis makes
  every process read/write the same cache, so scaling out workers doesn't
  introduce staleness.
- **Postgres for durable state**, with connection pooling via SQLAlchemy
  (`pool_pre_ping=True`), so the actual source of truth survives process
  restarts and is shared across however many app instances are running.
- **Voice calls are the one piece that doesn't horizontally scale the same
  way** — a live `SmallWebRTCConnection` is pinned to whichever process
  accepted its offer (the aiortc peer connection object lives in that
  process's memory; you cannot hand an in-progress call to another process).
  This is a fundamental property of P2P WebRTC, not a gap in this codebase.
  In practice this means: scale voice traffic by routing *new* connections
  across instances (e.g., round-robin at the load balancer — each call is
  independent and short-lived), not by sharing one call's state.
- **Cache TTLs bound memory growth.** Active-session entries expire after 30
  minutes of inactivity; the leaderboard cache refreshes every 60 seconds.
  Neither cache grows unbounded regardless of how many sessions have ever
  existed.

---

## 5. Fault tolerance and resilience

- **Validation is never a network call.** `engine.evaluate()` is pure,
  in-process Python — no dependency on Deepgram or Groq being up decides
  whether an answer was scored correctly. If Groq's API were to fail entirely,
  the game would keep validating and scoring correctly; only the host's
  *banter* would degrade (or the pipeline would surface an error frame for
  that one utterance), never the correctness of scoring.
- **The exact word sequence is never at the mercy of the LLM.** Even a
  hallucinating or slow LLM cannot corrupt what the user is asked to repeat,
  because that path (`TTSSpeakFrame`) never touches the LLM stage at all.
- **Concurrent double-submission cannot corrupt state**, even under real
  network-level races (see §6 for the mechanics) — a losing request returns
  the winner's result instead of raising a 500.
- **A DB-level `UniqueConstraint` on `Response.round_id`** is the final
  backstop under *all* of the above: even if application logic had a bug,
  the database itself physically cannot hold two scored responses for the
  same round.
- **Tests never depend on external services** — Postgres is swapped for an
  isolated in-memory SQLite database, and Redis is swapped for `fakeredis`.
  This means correctness can be verified (and was, repeatedly, throughout
  development) even when Postgres/Redis/Deepgram/Groq are all unavailable,
  which is also what makes CI/CD for this project cheap and fast.
- **Idempotent session lifecycle.** Ending an already-ended session, or
  resuming a session via the voice pipeline that the REST API already
  created, are both safe no-ops / correct resumes rather than errors — see
  `GameService.end_session` and `MemoryGameProcessor._start_session_sync`.

---

## 6. Concurrency

Two distinct concurrency problems were identified and fixed during
development (both are real commits in this repo's history, not hypothetical):

### 6.1 Same-round double-submission race

`GameService.submit_answer` originally read the `Round`, checked whether it
was already answered, and only *then* wrote — a classic check-then-act race.
Under real concurrency (e.g., a flaky client double-firing, or two voice
turns overlapping), two requests could both read the round as unanswered
before either committed. The DB's unique constraint on `Response.round_id`
correctly prevented the second write from corrupting data, but the
application wasn't catching that failure — the loser surfaced an unhandled
`IntegrityError` (a 500) instead of behaving idempotently.

**Fix:** the write path is wrapped in `try/except IntegrityError`. On
conflict, the losing request rolls back its own attempt, re-fetches the
round, and returns the *winning* request's already-computed result — the
same contract the ordinary idempotency check already provides for sequential
resubmissions, now also guaranteed under true concurrency. Covered by
`tests/test_service.py::test_submit_answer_recovers_from_concurrent_insert_conflict`,
which deliberately constructs the race window and asserts no exception
escapes.

### 6.2 Cross-process cache consistency

Covered in §4 — the move from an in-process cache to Redis is as much a
concurrency fix as a scalability one: without it, two processes serving the
same session concurrently (e.g., one REST request and one voice-pipeline
write landing on different workers) could each cache a different, stale view
of that session's score/round.

### 6.3 One voice call per session, enforced by construction

The voice pipeline resumes the exact session the REST API created
(`session_id` is threaded through `POST /rtc/offer` → `run_bot()` →
`MemoryGameProcessor`) rather than starting a second, independent session.
This isn't just a UX bug fix (the frontend's polling would otherwise never
reflect the voice pipeline's writes) — it also means there is exactly one
writer path per session at the application level, which is what makes the
race in §6.1 a narrow, well-understood edge case rather than a systemic
multi-writer problem.

### 6.4 Per-connection isolation

Each `SmallWebRTCConnection`/pipeline instance is independent — one player's
voice call, `MemoryGameProcessor`, and DB session have no shared mutable
state with another player's. Concurrent players don't contend for any
in-process lock or shared object; the only shared, contended resources are
Postgres (which enforces correctness via constraints) and Redis (which is
simply key-value and doesn't need locking for this access pattern).

---

## 7. Known limitations / explicit non-goals

- **Voice calls don't survive a process restart or fail over to another
  instance.** This is inherent to peer-to-peer WebRTC, not a fixable
  application bug — a new call would need to be established.
- **No horizontal autoscaling wiring (e.g., Kubernetes HPA, load balancer
  config) is included** — the codebase is structured to *support* running
  multiple stateless workers (Redis + Postgres backing), but provisioning
  that infrastructure is out of scope for this assignment.
- **No rate limiting / abuse protection** on session creation — acceptable
  for the assignment's scope, would be a first addition before any public
  deployment.
