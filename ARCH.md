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

---

## 8. Mapping this project to a Senior/AI Software Engineer interview (Curelink JD)

This section is deliberately honest about scale: this is a single-machine
assignment project, not a system running at Curelink's 350K MAU / 150K
minutes-of-voice-per-day scale. The value here is that most of the *individual
engineering decisions* below are the same shape as what shows up at any
scale — just with smaller numbers. Use the "evidence" as concrete, specific
things you actually built and can walk through line-by-line; use the "at
scale" notes as the honest extrapolation an interviewer will push you toward.

### 8.1 "LLM orchestration at scale (latency, cost, memory)"

**Evidence in this codebase:**
- The LLM is deliberately kept **off the critical correctness path** —
  `engine.evaluate()` (pure Python) judges the answer; Groq only ever
  generates host banter (`app/voice/game_processor.py`, `_banter()`). This is
  a latency *and* cost decision: every round makes exactly one small LLM call
  for flavor text, never a call whose failure or slowness could block or
  corrupt game state.
- The system prompt (`HOST_SYSTEM`) is deliberately one line, asking for "ONE
  short, energetic sentence" — a direct token-budget/latency control, not an
  afterthought.
- The exact word sequence bypasses the LLM stage entirely via a separate
  `TTSSpeakFrame` channel (`_speak()` vs `_banter()`) — this is the "memory"
  half of LLM orchestration: deciding what the model is and isn't allowed to
  be the source of truth for.

**At scale, be ready to talk about:** prompt/response caching for repeated
banter patterns, streaming partial LLM tokens into TTS to cut perceived
latency (this project doesn't stream — it waits for the full LLM response
before TTS starts), fallback behavior when the LLM provider is degraded
(this project has none — an LLM outage would surface as a pipeline error
frame, not a graceful "skip banter, just play the sequence" fallback), and
cost controls like model tiering (cheap model for banter, escalate only if
needed) — none of which is built here but all of which follow directly from
the separation already in place.

### 8.2 "Voice AI infra (real-time calls, async workflows)"

**Evidence in this codebase — this is the strongest match:**
- A full real-time WebRTC voice pipeline: `app/voice/bot.py` (pipeline
  assembly), `app/voice/webrtc.py` (SDP offer/answer signaling),
  `app/voice/game_processor.py` (custom turn-taking and barge-in logic).
- **A genuine, non-trivial debugging story**: the bot initially never
  reacted to answers at all. Root-caused by adding targeted diagnostic
  logging (frame-type tallies) rather than guessing, which showed audio
  frames flowing and Deepgram transcribing correctly, but the turn never
  activating. Traced into Pipecat's transport source to find that
  `VADUserStartedSpeakingFrame`/`VADUserStoppedSpeakingFrame` (not the
  plain-named frames) are what this pipeline configuration actually emits —
  a version-specific, undocumented-in-practice framework detail found by
  reading source, not docs.
- **A second real debugging story, same category**: barge-in silently didn't
  work — the bot talked over the user with no error. Traced through the
  transport's VAD-handling code paths to find that `StartInterruptionFrame`
  is *only* ever emitted via a deprecated turn-analyzer path or an
  `LLMUserAggregator`, neither of which this minimal pipeline uses. Fixed by
  having the custom processor track bot-speaking state itself and call the
  framework's own `broadcast_interruption()` — using a *stable* public API
  correctly, instead of assuming a framework default that silently didn't
  apply to this configuration.
- Async workflow handling: blocking DB calls inside the voice pipeline are
  explicitly run via `asyncio.to_thread(...)` (`_start_session_sync`,
  `_submit_sync`) so they never stall the audio event loop — a direct,
  concrete example of sync/async boundary management in a latency-sensitive
  path.

**At scale, be ready to talk about:** call routing/load-balancing for voice
specifically (§4 above — WebRTC calls are pinned to a process by nature),
horizontal scaling of STT/TTS vendor concurrency limits, and turn-detection
tuning for real-world noisy audio (this project uses default Silero VAD
thresholds).

### 8.3 "High-throughput backend systems (queues, pods, autoscaling)"

**Evidence in this codebase:** the REST layer is deliberately stateless (§4)
— every request re-derives state from Redis/Postgres, which is *the*
precondition for running many pods behind a load balancer with no sticky
routing. The Redis migration (§4, §6.2) was made *specifically* because the
original in-process cache would silently break correctness the moment you
ran more than one worker — i.e., this project has a real, committed example
of "single-instance code that looks fine until you scale it," found and
fixed before it shipped.

**Honest gap:** there is no message queue anywhere in this codebase, and no
ECS/Kubernetes deployment — it runs as a single Uvicorn process against
docker-compose Postgres/Redis. Don't imply otherwise. Do be ready to describe
*where* a queue would go if this needed to scale: e.g., moving the Groq
banter call off the request-latency path entirely (publish a "round resolved"
event, consume it asynchronously, push the result over the WebRTC data
channel or a websocket when ready) rather than awaiting it inline as
`_banter()` does today. That's a concrete, defensible extension of this
exact codebase, not a generic answer.

### 8.4 "Cost-efficient cloud infra (AWS/GCP)"

**Evidence in this codebase:** fully 12-factor-style config — every
credential and endpoint (`DATABASE_URL`, `REDIS_URL`, API keys) comes from
environment variables via `pydantic-settings` (`app/config.py`), nothing
hardcoded, `.env` is git-ignored. Postgres and Redis both run as ordinary
containers (`docker-compose.yml`) with no code assuming a specific
deployment target — the same image would run unmodified on ECS Fargate, GKE,
or a bare VM.

**Honest gap:** no actual cloud deployment exists for this project. Be ready
to talk about the *shape* of a cost-efficient deployment of this exact
system: stateless API pods on spot/preemptible instances (safe because they
hold no session state — Redis does), a managed Postgres (RDS/Cloud SQL)
sized for the actual write pattern (session/round/response inserts are small
and infrequent per user), and a managed Redis (ElastiCache/Memorystore) sized
by session TTL × concurrent players rather than guessed.

### 8.5 "API/data model design for messy real-world domains, adapting to changing requirements"

**Evidence in this codebase:** the `SessionState` schema was extended twice
during development (word-visibility fields, then nothing removed) —
`last_expected`/`last_heard`/`last_correct` were added as **purely additive,
optional fields** (`app/schemas.py`), so no existing consumer (the REST API
tests, the frontend) had to change to keep working. The `AnswerResult` vs
`SessionState` split itself is a real modeling decision: `SessionState` never
leaks the *current* unanswered sequence (only its length), while
`AnswerResult`/the new `last_*` fields deliberately reveal words only *after*
they're already answered — the schema encodes a game rule, not just data
shape.

### 8.6 "Operating and debugging infra — DBs, caches, load balancers, deployments"

**Evidence in this codebase — two real, resolved incidents, both found by
inspecting actual running processes rather than assuming the code was
wrong:**
- Postgres connections failed with `role "voiceflash" does not exist` —
  root-caused to a **native Postgres process already bound to port 5432** on
  the dev machine, silently shadowing the intended Docker container. Found
  via `lsof -nP -iTCP:5432 -sTCP:LISTEN`, not by reading application code.
  Same pattern recurred with Redis on 6379. Both resolved by remapping the
  container ports (5433, 6380) rather than fighting the OS.
- An API credential ("Deepgram key") failed with 401s that *looked* like a
  config-loading bug; verified directly against the vendor's REST API
  (`GET /v1/projects`) to prove the key itself was server-side deactivated,
  not a bug in this code — the discipline of checking the boundary
  (is it us or is it them?) with a live, minimal repro instead of guessing.

### 8.7 "Strong database fundamentals, query optimization, EXPLAIN plans"

**Honest gap:** this project's data volume never approached a scale where
query optimization mattered — no query in this codebase has been profiled
with `EXPLAIN ANALYZE`, and no index was added beyond what a primary key /
foreign key implies. Don't overclaim this from the project itself. What *is*
real and defensible: the schema design that makes the common queries cheap
by construction — `Round` is filtered by `(session_id, round_number)`
(`GameService._current_round`) and `Response` is uniquely keyed by
`round_id` — both are exactly the access patterns a composite index would
target at scale, even though none was added here because it wasn't needed
yet. Bring a *different*, real story for the EXPLAIN-plan/outage question if
you have one; don't stretch this project to cover it.

### 8.8 "System design fundamentals: sync vs async, concurrency, ordering, failure modes"

**Evidence in this codebase — this is the second-strongest match, alongside
§8.2:**
- **A real concurrency bug, found and fixed, not hypothetical**: a
  check-then-act race in `GameService.submit_answer` where two near-
  simultaneous submissions for the same round could both pass an
  idempotency check before either committed. Fixed with a DB unique
  constraint as the actual correctness guarantee, plus application-level
  `IntegrityError` handling so the losing request degrades gracefully
  instead of surfacing a 500 (§6.1). This is a textbook "failure mode in a
  distributed system" — multiple writers, no distributed lock, correctness
  enforced by the storage layer instead.
- **Ordering**: the turn-taking logic (§3.2) is fundamentally an ordering
  problem — two async signals (VAD stop, STT final transcript) can arrive in
  either order, and the system is explicitly written to be correct
  regardless of which arrives last, rather than assuming a fixed order.
- **Sync vs async trade-offs, made explicit and justified**: blocking
  SQLAlchemy calls are pushed to a thread pool (`asyncio.to_thread`) from the
  latency-sensitive voice path so they never stall the audio pipeline,
  while the REST layer uses FastAPI's ordinary sync-in-threadpool handling —
  two different concurrency models chosen deliberately for two different
  latency profiles in the same codebase.
- **Idempotency as a first-class design goal**, not a patch: ending an
  already-ended session, resubmitting an already-answered round, and the
  voice pipeline resuming a REST-created session instead of forking a new
  one are all explicit idempotent/resumable code paths, not accidents.

---

## 9. End-to-end flow reference — where to look, in order

Use this as a navigation map when asked "walk me through what happens
when...". Each row is the exact call order, file by file.

### 9.1 Starting a game (full voice path)

1. `static/app.js` `startGame()` — `POST /api/sessions`
2. `app/api/routes.py` `start_session()` → `app/game/service.py`
   `GameService.start_session()` → `app/game/engine.py`
   `generate_sequence()` (round 1 = 3 words) → commits `GameSession` + `Round`
   → `app/cache/store.py` `set_active_session()` (Redis)
3. Browser opens `RTCPeerConnection`, captures mic, creates SDP offer
4. `static/app.js` `startGame()` — `POST /rtc/offer` with `{sdp, type,
   player_name, session_id}` (the `session_id` from step 2 — this is the
   link that makes §6.3 work)
5. `app/voice/webrtc.py` `offer()` — creates `SmallWebRTCConnection`, builds
   `SmallWebRTCTransport`, schedules `run_bot()` as a background task,
   returns the SDP answer
6. `app/voice/bot.py` `run_bot()` — assembles the Pipecat pipeline, registers
   `on_client_connected` → calls `MemoryGameProcessor._start_game()`
7. `app/voice/game_processor.py` `_start_game()` →
   `_start_session_sync()` → **resumes** the session from step 2 via
   `GameService.get_state()` (not a new `start_session()` call) → `_banter()`
   (Groq greeting) → `_speak()` (deterministic `TTSSpeakFrame` of the actual
   sequence)

### 9.2 Answering a round (voice path)

1. User speaks → Deepgram STT streams back a transcript
2. Pipecat transport's VAD path pushes `VADUserStartedSpeakingFrame`, then
   (whenever it arrives) `TranscriptionFrame`, then
   `VADUserStoppedSpeakingFrame` — order between the last two is not
   guaranteed (§3.2, §8.8)
3. `app/voice/game_processor.py` `process_frame()` accumulates transcript
   text in `_buffer`; `_finish_turn()` fires once **both** the stop signal
   and buffered text are present
4. `_handle_user_turn()` → `_submit_sync()` (off the event loop via
   `asyncio.to_thread`) → `GameService.submit_answer()`
5. `app/game/service.py` `submit_answer()` → `engine.evaluate()` (pure,
   deterministic) → persists `Response`, updates `GameSession.score`/
   `current_round`, refreshes/invalidates Redis (§6.1 covers the race here)
6. Back in `game_processor.py`: `_banter()` (Groq reacts to the result) →
   `_speak()` (next sequence, or the reveal line if the game ended)

### 9.3 Answering a round (REST path, used by tests / manual verification)

`POST /api/sessions/{id}/answer` → `app/api/routes.py` `submit_answer()` →
`GameService.get_state()` (to find the current `round_id`) →
`GameService.submit_answer()` — **same function as the voice path**, so
everything in 9.2 step 5 applies identically here.

### 9.4 Reading state (frontend polling)

`static/app.js` `refreshState()` (every 1.5s) → `GET /api/sessions/{id}` →
`app/api/routes.py` `get_state()` → `GameService.get_state()` — cache-first
against Redis (`app/cache/store.py` `get_active_session()`), falls back to
Postgres only on a cache miss, then re-warms the cache.

### 9.5 Leaderboard

`GET /api/leaderboard` → `GameService.leaderboard()` — cache-first against
Redis (`get_leaderboard()`, 60s TTL); on miss, queries `GameSession` ordered
by `score DESC, created_at ASC`, then calls `set_leaderboard()` to refresh
the cache. Invalidated (`invalidate_leaderboard()`) by `submit_answer()` and
`end_session()` whenever a score could have changed.

### 9.6 Interruption / barge-in

User talks while the bot is mid-speech → transport's VAD path pushes
`VADUserStartedSpeakingFrame` → `game_processor.py` `process_frame()` sees
`self._bot_speaking is True` (tracked via `BotStartedSpeakingFrame`/
`BotStoppedSpeakingFrame`, which broadcast upstream from
`transport.output()`) → calls `self.broadcast_interruption()` → pushes a
plain `InterruptionFrame` both directions → every processor's base
`process_frame()` (Pipecat framework code, not ours) checks
`isinstance(frame, InterruptionFrame)` and stops in-flight work — this is
what actually silences the TTS/audio output.

### 9.7 The concurrency race (double-submission)

`app/game/service.py` `submit_answer()`: two calls both pass the
`rnd.status != "PENDING"` check (both read `PENDING`) → both build a
`Response` and attempt to write → the DB's `UniqueConstraint` on
`Response.round_id` (`app/db/models.py`) lets exactly one commit succeed →
the loser's `flush()`/`commit()` raises `IntegrityError`, caught in the
`except IntegrityError:` block → rolls back, re-fetches, calls
`_result_from_stored()` to return the winner's result. Test:
`tests/test_service.py::test_submit_answer_recovers_from_concurrent_insert_conflict`.

---

## 10. Grill questions to expect — with honest answers

These are the follow-up questions an interviewer would actually ask after
hearing §8's talking points. Several of the answers are **real gaps found
while re-reading this exact code just now** — say them as gaps, not as
solved problems; that honesty is worth more than a confident non-answer.

**Q: What happens if Redis goes down mid-game?**
Right now: it breaks. `app/cache/store.py` has no try/except around any
`_redis.*` call — `get_active_session`/`set_active_session`/etc. would raise
a connection error straight up through `GameService`, surfacing as a 500 on
every request and crashing the voice pipeline's background thread calls.
The correct fix is a fallback-to-Postgres-on-cache-error path (treat Redis
as an optimization, never a hard dependency for correctness) — not built
here, and worth saying so directly.

**Q: What happens if a client sends a `session_id` in `/rtc/offer` that
doesn't exist?**
Also breaks ungracefully today: `MemoryGameProcessor._start_session_sync()`
calls `GameService.get_state()` with no try/except; `get_state` raises
`KeyError` for an unknown session, which propagates out of
`asyncio.to_thread` uncaught, likely tearing down that voice connection
without ever telling the user why. The fix would be to catch `KeyError`
there and fall back to `start_session()` (or reject the call cleanly) —
same category of gap as the Redis one: an internal invariant that isn't
defensively checked at a trust boundary.

**Q: Why is there no ownership/auth check tying a `session_id` to the
browser that created it? Could I steal someone else's session?**
Yes, as built. Any client that obtains a valid `session_id` (e.g., by
inspecting network traffic) could pass it to `/rtc/offer` or
`/api/sessions/{id}/answer` and act on someone else's game. There's no
per-session secret/token, no auth at all. Acceptable for this assignment's
scope (no real user accounts exist); the fix at any real scale is a signed
session token (or simply requiring auth and scoping sessions to a user id)
checked at the API boundary, not something bolted onto `GameService`.

**Q: Why catch `IntegrityError` instead of using `SELECT ... FOR UPDATE` to
lock the row before checking?**
Deliberate optimistic-concurrency choice: row locking would serialize every
submission attempt (even the overwhelming majority that never race) behind
a held lock for the duration of evaluation + write, adding latency to the
common case to protect against a rare case. Catching the constraint
violation costs nothing on the non-racing path and only pays a (cheap,
one-time) rollback+refetch on the rare actual race. This is the standard
optimistic-vs-pessimistic-locking trade-off, applied because double-
submission is rare, not because locking is wrong in general — for a
resource with heavy contention (not this one), locking or a queue would
likely win instead.

**Q: The DB constraint is the real guarantee — why bother with the
application-level idempotency check (`rnd.status != "PENDING"`) at all?**
Because the DB constraint only prevents *data corruption*; it doesn't, by
itself, give the *caller* a correct response. Without the application check,
a normal sequential resubmission (not a race — just the same client
retrying, e.g., after a flaky network response) would still hit the
constraint and raise, forcing every retry through the exception path. The
application-level check makes the common, non-racing repeat-request case
fast and clean; the exception path is a safety net for the rare true race,
not the primary mechanism.

**Q: Why poll every 1.5s from the frontend instead of pushing updates over
a WebSocket or the WebRTC data channel that's already open?**
Simplicity for this scope — polling needs no additional protocol and
degrades trivially (a missed poll just means a 1.5s-stale UI, never a stuck
one). At real scale, polling is the thing to replace first: it costs one
Postgres/Redis round-trip per connected client every 1.5 seconds regardless
of whether anything changed, whereas a push-based channel (the WebRTC data
channel is already there, unused for this) would only send data when state
actually changes.

**Q: What happens if the user never stops talking — does the round hang
forever?**
Yes, currently. `game_processor.py`'s turn-finishing logic only fires once a
stop signal arrives; if the VAD analyzer never detects silence (e.g.,
continuous background noise), the turn simply waits with no timeout. The
fix is a max-turn-duration timer that force-finishes the turn — not
implemented here.

**Q: Why does `engine.normalize()` strip filler words like "the"/"um" —
what could that break?**
It's a precision/recall trade-off for STT noise. If a real target word were
ever one of the filler words (currently: `um, uh, the, a, an, and, then,
was, is, please, okay`), it would be silently un-checkable — always
stripped before comparison. In this project it's safe because the word pool
(`app/game/words.py`) is curated to never include those words, but that's an
implicit invariant between two files, not enforced by any test or
assertion.

**Q: The leaderboard cache is 60 seconds, the session cache is 30 minutes —
how were those numbers chosen?**
Not empirically tuned — reasonable defaults for a fast-paced game (a round
resolves in seconds, so 30 minutes generously covers someone mid-game
without letting abandoned sessions accumulate in Redis forever; 60 seconds
keeps the leaderboard feeling near-live without hitting Postgres on every
single leaderboard view). Be ready to say exactly that if pressed — these
weren't load-tested.
