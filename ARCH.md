# Architecture

This document explains the system design of the Memory Card Voice Bot: what each
piece is, why it was chosen, and specifically how the system behaves under
multiple simultaneous users, partial failures, and concurrent writes. For setup
instructions and gameplay, see [README.md](README.md).

---

## 1. Goal and constraints

**Simple version:** it's a "Simon Says" memory game played by voice. The bot
says a list of words, you repeat them back, and code (never an AI model)
decides if you got it right.

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

**Simple version:** one Python web server does everything — talks to the
voice AI providers, stores game data in a real database, keeps a fast cache
in Redis, and serves a plain HTML page. Nothing exotic; every choice below
is the boring, standard option for what it's doing.

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

**Simple version:** the browser talks to the server two different ways at
once — a normal REST API for starting/checking/ending the game, and a
separate live voice connection (WebRTC) for the actual talking. Both paths
end up calling the exact same game logic underneath, so there's only ever
one "brain" deciding what's correct.

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

**Simple version:** all the actual game rules live in one file
(`engine.py`), and one wrapper (`service.py`) is the only thing allowed to
save that to a database. Both the website and the voice bot call the same
wrapper — so there's no way for the two to disagree about what happened.

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

**Simple version:** audio flows through a straight line of stages — speech
becomes text, my code decides what to do, an AI model adds personality,
text becomes speech again. The two trickiest real-time problems (knowing
when someone's *done* talking, and reacting instantly if they talk over the
bot) aren't handled automatically by the framework here — they're solved by
hand in this file, and that's the most interesting part of this project to
talk about.

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

**Simple version:** the website/API part could run on 10 servers at once
tomorrow with zero code changes, because none of them remember anything
themselves — Postgres and Redis do. The one thing that *can't* just be
copied across servers is a live phone call, and that's true of any
real-time voice system, not a flaw here.

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

**Simple version:** if any single piece of this system breaks — the AI
voice provider, the LLM, even the cache — the game either keeps working
correctly or fails safely, never silently wrong. Several of the bullets
below are real bugs that were found and fixed during development, not
theoretical "what if" scenarios.

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
- **Redis is an optimization, never a hard dependency.** Every function in
  `app/cache/store.py` catches `redis.exceptions.RedisError`: reads degrade
  to "cache miss" (falling back to Postgres), writes are best-effort and
  simply log a warning. A Redis outage makes the app slower, never broken —
  this was a real gap (found during a code review pass, not hypothetical)
  that's now fixed and covered by `tests/test_cache.py`'s
  `*_on_redis_outage` tests.
- **An unrecognized `session_id` from a voice client no longer crashes the
  call.** `MemoryGameProcessor._start_session_sync` used to let `KeyError`
  propagate uncaught out of `asyncio.to_thread`, silently tearing down the
  connection. It now catches that specific case and falls back to starting
  a fresh session for that player — tested in
  `tests/test_game_processor.py::test_start_session_sync_falls_back_when_session_id_unknown`.
- **A turn no longer waits forever — and the timeout scales with how much
  there is to say.** If VAD's stop signal never arrives (e.g. persistent
  background noise), a watchdog (`MemoryGameProcessor._turn_timeout_watchdog`)
  force-resolves the turn instead of leaving the round stuck in silence
  indefinitely. `_turn_timeout_for(sequence_length)` = 6s base + 2.5s per
  word — a flat timeout was found to cut players off mid-answer on longer,
  harder rounds (a 6-word sequence legitimately takes longer to say than a
  3-word one), so round 1 gets ~13.5s while round 4 gets ~21s.
- **A page refresh no longer loses the game.** `static/app.js` saves the
  active `session_id` to `sessionStorage` and checks it on load; if that
  session is still `ACTIVE` server-side, a "Welcome back" banner offers
  resuming (reconnects voice to the *same* session, using the exact
  session-linking mechanism from §6.3) or explicitly ending the old session
  via `POST /api/sessions/{id}/end` before starting fresh — closing the gap
  where an abandoned session used to sit as an orphaned `ACTIVE` row in
  Postgres forever.

---

## 6. Concurrency

**Simple version:** "concurrency" here just means "what if two things
happen at almost exactly the same time." Two real cases of that were found
and fixed in this project — not made up for this document.

Two distinct concurrency problems were identified and fixed during
development (both are real commits in this repo's history, not hypothetical):

### 6.1 Same-round double-submission race

**Simple version:** if the same answer got submitted twice in the same
split second, the game could have scored it twice. Now it can't — the
database itself refuses to let that happen, and the code handles that
refusal gracefully instead of crashing.

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

**Simple version:** if you ran two copies of this server, they'd need to
agree on each player's score at all times. That only works because the
cache is Redis (shared), not each server's own memory (which wouldn't agree).

Covered in §4 — the move from an in-process cache to Redis is as much a
concurrency fix as a scalability one: without it, two processes serving the
same session concurrently (e.g., one REST request and one voice-pipeline
write landing on different workers) could each cache a different, stale view
of that session's score/round.

### 6.3 One voice call per session, enforced by construction

**Simple version:** the website and the voice bot both operate on the same
game session — the voice bot never quietly creates a second, disconnected
one behind the scenes (this was an actual bug, found and fixed).

The voice pipeline resumes the exact session the REST API created
(`session_id` is threaded through `POST /rtc/offer` → `run_bot()` →
`MemoryGameProcessor`) rather than starting a second, independent session.
This isn't just a UX bug fix (the frontend's polling would otherwise never
reflect the voice pipeline's writes) — it also means there is exactly one
writer path per session at the application level, which is what makes the
race in §6.1 a narrow, well-understood edge case rather than a systemic
multi-writer problem.

### 6.4 Per-connection isolation

**Simple version:** two players' games never touch each other's data in
memory — the only place they could possibly collide is the shared database,
which is exactly where §6.1's fix lives.

Each `SmallWebRTCConnection`/pipeline instance is independent — one player's
voice call, `MemoryGameProcessor`, and DB session have no shared mutable
state with another player's. Concurrent players don't contend for any
in-process lock or shared object; the only shared, contended resources are
Postgres (which enforces correctness via constraints) and Redis (which is
simply key-value and doesn't need locking for this access pattern).

---

## 7. Known limitations / explicit non-goals

**Simple version:** here's what this project honestly doesn't do, said
plainly instead of buried. Naming your own gaps unprompted is a stronger
interview signal than waiting to be caught out.

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
- **No ownership/auth on `session_id`.** Anything that knows a valid
  `session_id` can act on that session via the REST API or a voice call —
  there's no per-session secret or user account tying a session to whoever
  created it. Deliberately not fixed alongside the other gaps in §5/§10,
  because it's a real design decision (session tokens vs. full auth), not a
  contained bug fix — see §10 for the fuller discussion.

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

**Simple version:** this is a map for "walk me through what happens when a
user does X" questions. Each numbered flow below is a literal ordered list
of which file and which function runs next — read top to bottom and you're
reading the actual code path.

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
Redis (`get_leaderboard()`, 60s TTL); on miss, queries `GameSession` **grouped
by `player_name`, taking `MAX(score)`** (so a player who's played multiple
sessions appears once, ranked by their best run — not once per session),
tie-broken by `MIN(created_at) ASC`, then calls `set_leaderboard()` to
refresh the cache. Invalidated (`invalidate_leaderboard()`) by
`submit_answer()` and `end_session()` whenever a score could have changed.

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
hearing §8's talking points. A few were **real gaps found while re-reading
this exact code**, and have since been fixed — narrated below as
before/after, because "I found this, here's why it mattered, here's the fix
and its test" is a stronger interview answer than either "it's perfect" or
a static list of flaws.

**Q: What happens if Redis goes down mid-game?**
Originally: it broke everything. `app/cache/store.py` had no try/except
around any `_redis.*` call, so a connection error would raise straight up
through `GameService`, surfacing as a 500 on every cache-touching request
and crashing the voice pipeline's background thread calls too. **Fixed**:
every function in `store.py` now catches `redis.exceptions.RedisError` —
reads degrade to "cache miss" (Postgres is the fallback, already the
correct behavior `GameService.get_state` expected for ordinary misses),
writes are best-effort and just log a warning. Covered by
`tests/test_cache.py`'s `test_*_on_redis_outage` tests, which simulate a
Redis client where every call raises. Redis is now genuinely an
optimization, never a hard dependency for correctness.

**Q: What happens if a client sends a `session_id` in `/rtc/offer` that
doesn't exist?**
Originally: also broke ungracefully. `MemoryGameProcessor._start_session_sync()`
called `GameService.get_state()` with no try/except; `get_state` raises
`KeyError` for an unknown session, which propagated out of
`asyncio.to_thread` uncaught, tearing down that voice connection without
ever telling the user why. **Fixed**: that specific `KeyError` is now
caught, logged as a warning, and falls back to starting a fresh session for
that player — the call degrades to "you get a new game" instead of dying.
Covered by
`tests/test_game_processor.py::test_start_session_sync_falls_back_when_session_id_unknown`.

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
Originally: yes. `game_processor.py`'s turn-finishing logic only fired once
a stop signal arrived; if VAD never detected silence (e.g. continuous
background noise), the turn simply waited forever with no feedback.
**Fixed**: `_turn_timeout_watchdog()` starts an 8-second timer whenever a
turn begins (`_TURN_TIMEOUT_SECS`), cancelled if the turn finishes normally;
if it fires, it force-resolves the turn with whatever's been heard so far.
Writing its test surfaced a second, subtler bug: the watchdog calling
`_finish_turn()` → `_reset_turn()` → `_cancel_turn_timeout()` would cancel
*itself* (it's running inside that very task), raising `CancelledError` out
of the turn it had just resolved. Fixed by checking
`task is not asyncio.current_task()` before cancelling — a good concrete
example of a self-referential concurrency bug that only shows up once you
actually write the test instead of reasoning about it on paper.

**Q: The leaderboard showed the same player multiple times — why, and how
was it fixed?**
`GameService.leaderboard()` originally queried `GameSession` rows directly
with no grouping — since a player can start a new session every time they
play, a player who'd played 3 times showed up as 3 separate leaderboard
rows instead of one. **Fixed**: the query now groups by `player_name` and
takes `MAX(score)`, so each player appears exactly once, ranked by their
personal best. This is the same "session-per-play vs. identity-per-player"
modeling gap that shows up constantly in real systems — the schema
(`GameSession` rows) models *attempts*, but the leaderboard needs to answer
a question about *players*, and conflating the two is an easy, easy-to-miss
bug.

**Q: Why does `engine.normalize()` strip filler words like "the"/"um" —
what could that break?**
It's a precision/recall trade-off for STT noise. If a real target word were
ever one of the filler words (currently: `um, uh, the, a, an, and, then,
was, is, please, okay`), it would be silently un-checkable — always
stripped before comparison. In this project it's safe because the word pool
(`app/game/words.py`) is curated to never include those words — this used to
be an implicit, undocumented invariant between two files; it's now enforced
by `tests/test_engine.py::test_word_pool_never_overlaps_filler_words`, so
adding a word like "the" to the pool by accident would fail CI instead of
silently breaking gameplay.

**Q: The leaderboard cache is 60 seconds, the session cache is 30 minutes —
how were those numbers chosen?**
Not empirically tuned — reasonable defaults for a fast-paced game (a round
resolves in seconds, so 30 minutes generously covers someone mid-game
without letting abandoned sessions accumulate in Redis forever; 60 seconds
keeps the leaderboard feeling near-live without hitting Postgres on every
single leaderboard view). Be ready to say exactly that if pressed — these
weren't load-tested.

**Q: What happens if I refresh the page mid-game?**
Originally: the game was silently lost. `sessionId` lived only in a JS
variable, so a refresh forgot it completely — the frontend had no way back
to that session, and the old `GameSession` row just sat in Postgres forever,
still `ACTIVE`, never explicitly ended. **Fixed**: the session ID is saved
to `sessionStorage` (per-tab — deliberately not `localStorage`, so it
doesn't collide with the two-tabs-one-device testing workflow from §11) and
checked on load. If that session is still `ACTIVE`, the player gets a
"Welcome back" choice: resume (reconnect voice to the same session — this
works *because* of the session-linking fix in §6.3, which already made the
backend able to resume an existing session_id) or explicitly end the old
one via the real API before starting fresh, so nothing gets abandoned
silently anymore.

**Q: How would you actually test that this holds up with multiple
concurrent voice users, given you don't have a fleet of phones or
microphones sitting around?**
Two different tools for two different layers, and it's worth being explicit
about which one tests what — see §11 for the full walkthrough:
- The backend's concurrency correctness (Postgres, Redis, the race-condition
  fix in §6.1) is testable with **pure automated load** — fire many
  simultaneous `POST /api/sessions` + `/answer` requests with `asyncio`/
  `httpx` against a running server. No microphone involved, and it's exactly
  what `tests/test_service.py::test_submit_answer_recovers_from_concurrent_insert_conflict`
  already does at the unit level, just scaled up against a live server
  instead of monkeypatched in-process.
- The **voice-specific** concurrency question — do two simultaneous
  real-time STT/TTS streams interfere with each other — genuinely needs live
  audio, and has a physical constraint that's easy to miss until you try it:
  on one device, multiple browser tabs share the **same physical
  microphone**, so both bots hear whatever's said in the room regardless of
  which tab has focus. That's not a server bug, it's a property of testing
  voice systems specifically (unlike testing a typical REST API, where a
  second "client" is just another terminal). The real test needs either two
  separate devices/mics, or turn-taking discipline on one device.

---

## 11. Testing multi-user scenarios in practice

**Simple version:** "does it work with multiple people" is really two
separate questions — does the database/cache stay correct under load
(testable with a script, no microphone needed), and does the actual voice
audio hold up with two real people talking at once (needs real hardware,
and has a physical gotcha worth knowing).

Two genuinely different tests, testing two different layers. Conflating
them (e.g. concluding "it handles concurrency" from only one of the two) is
the mistake to avoid.

### 11.1 Backend concurrency — automatable, no microphone needed

This is the layer §6 is about: does the database/cache stay correct under
simultaneous writers. Drive it with plain async HTTP load against a running
server:

```python
import asyncio, httpx

async def play_one(client, name):
    r = await client.post("/api/sessions", json={"player_name": name})
    sid = r.json()["session_id"]
    # ... fetch the real sequence from Postgres directly for a correct
    # answer, or just submit garbage to exercise the wrong-answer/game-over
    # path — either way, do this from N concurrent coroutines:
    return await client.post(f"/api/sessions/{sid}/answer",
                              json={"transcript": "..."})

async def main():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000") as client:
        results = await asyncio.gather(*[play_one(client, f"p{i}") for i in range(50)])
```

What to check afterward: every session's score in Postgres matches what its
own answers should have produced (no cross-contamination), the leaderboard
has exactly one row per name (§10's dedup fix), and Redis
(`redis-cli KEYS "session:*"`) has one key per session with no signs of a
crashed process (which the resilience fixes in §5 specifically guard
against).

### 11.2 Voice-specific concurrency — needs real audio, has a physical constraint

This is the layer that's actually hard to fake: do two simultaneous
real-time Deepgram STT/TTS streams (§3.2, §8.2) interfere with each other,
and does turn-taking/barge-in stay correct per-connection when two are
live at once.

- **Best: two separate devices on the same network**, each with its own
  microphone, both pointed at `http://<host-LAN-IP>:8000`. This is the only
  setup that tests truly simultaneous independent speech.
- **One device, multiple tabs**: works for proving connections/sessions
  stay independent server-side, but **all tabs share the one physical
  microphone** — whatever's said in the room reaches every open tab's STT
  pipeline, not just the "active" one. Not a bug in this codebase; it's an
  inherent property of testing voice systems on shared hardware, unlike a
  typical REST API where a second terminal is a fully independent client.
  Practical workaround with no code changes: strict turn-taking (only one
  person speaks at a time, alternating which tab they're "talking to").
- **One device, genuinely simultaneous speech**: needs two physical input
  devices (e.g. the laptop's built-in mic + a USB/headset mic) *and* a
  frontend change this codebase doesn't have yet — `static/app.js` calls
  `getUserMedia({ audio: true })` with no device selection, so every tab
  gets whichever mic the OS/browser defaults to. Supporting this would mean
  enumerating `navigator.mediaDevices.enumerateDevices()` and letting each
  tab pick a specific `deviceId` — a small, well-scoped addition if this
  becomes a recurring testing need, not built because it wasn't needed for
  the assignment's scope.

---

## 12. Assignment requirements in depth — exact structures for interview prep

**Simple version:** the assignment brief asked for three specific things —
a voice pipeline, a database with APIs, and caching. This section shows the
literal, exact structure that satisfies each one (real schema, real
endpoint list, real cache keys), not a summary of them.

The assignment brief (`src/requirements.txt`) states three requirements this
section maps directly against, with the *exact* current schema/contract for
each — not a paraphrase.

### 12.1 "Voice Bot Backend: a Pipecat-based pipeline that starts a memory
game, speaks a sequence, listens, validates, updates game state, and moves
to the next round or ends the game."

Each clause, mapped to exact code:

| Requirement clause | Exact code |
|---|---|
| Starts a memory game | `MemoryGameProcessor._start_game()` → `GameService.start_session()` (`app/game/service.py`) |
| Speaks a sequence | `MemoryGameProcessor._speak()` → `TTSSpeakFrame` (bypasses the LLM — §3.2) |
| Listens to the user repeat it | `process_frame()`'s `TranscriptionFrame`/VAD handling (§9.2) |
| Validates the response | `engine.evaluate()` (`app/game/engine.py`) — pure, deterministic, no LLM call |
| Updates game state | `GameService.submit_answer()` — persists `Response`, updates `GameSession.score`/`current_round` |
| Moves to next round or ends | Same function: `ev.is_correct` branches to `_new_round()` (next round) or `session.status = "ENDED"` (game over) |

The one-sentence version for the video: *"Every verb in that requirement
sentence is a specific function, and they're wired together in exactly that
order — nothing is implicit."*

### 12.2 "Backend APIs and Database"

**Exact database schema** (`app/db/models.py`, 3 tables):

```
game_sessions                        rounds                              responses
─────────────                        ──────                              ─────────
id             STRING PK (uuid)      id             STRING PK (uuid)     id              STRING PK (uuid)
player_name    STRING NOT NULL       session_id     STRING NOT NULL      round_id        STRING NOT NULL
status         STRING DEFAULT        round_number   INTEGER NOT NULL       FK -> rounds.id
  'ACTIVE'       (ACTIVE | ENDED)      FK -> game_sessions.id               UNIQUE (uq_response_per_round)
score          INTEGER DEFAULT 0     sequence       JSON NOT NULL        transcript      STRING NOT NULL
current_round  INTEGER DEFAULT 1       e.g. ["apple","tiger","river"]    normalized      JSON NOT NULL
created_at     TIMESTAMPTZ           status         STRING DEFAULT         e.g. ["apple","tiger","river"]
ended_at       TIMESTAMPTZ NULL        'PENDING'                         is_correct      BOOLEAN NOT NULL
                                        (PENDING | CORRECT | WRONG)      points_awarded  INTEGER DEFAULT 0
                                     created_at     TIMESTAMPTZ          created_at      TIMESTAMPTZ
```

Relationships: `game_sessions` 1—* `rounds` (`cascade="all, delete-orphan"`),
`rounds` 1—1 `responses` (same cascade). **The one constraint that matters
most in an interview**: `UniqueConstraint("round_id")` on `responses` — the
database physically cannot hold two scored responses for the same round,
which is the real guarantee behind "avoid double-scoring" (§6.1 has the
full race-condition story).

**Exact API contract** (`app/api/routes.py`, all backed by the same
`GameService` — §3.1):

| Method | Path | Request body | Response model | Notes |
|---|---|---|---|---|
| `POST` | `/api/sessions` | `{player_name: str}` | `SessionState` | Creates `GameSession` + round 1 |
| `GET` | `/api/sessions/{id}` | — | `SessionState` | Cache-first (Redis); `404` if unknown |
| `POST` | `/api/sessions/{id}/answer` | `{transcript: str}` | `AnswerResult` | `409` if no active round (session already ended) |
| `POST` | `/api/sessions/{id}/end` | — | `SessionState` | `404` if unknown; idempotent if already ended |
| `GET` | `/api/leaderboard?limit=10` | — | `list[LeaderboardEntry]` | Cache-first; deduped by player (§10) |

Response model shapes (`app/schemas.py`, Pydantic):

```python
SessionState:      session_id, player_name, status, score, current_round,
                    round_id (opt), sequence_length (opt),
                    last_expected (opt list), last_heard (opt list),
                    last_correct (opt bool)
                    # sequence_length is a length only — the *current*
                    # unanswered sequence never appears in any API response.
AnswerResult:       session_id, round_number, is_correct, points_awarded,
                    total_score, status, expected (list), heard (list)
                    # expected/heard ARE the actual words — but only ever
                    # for the round just answered, revealed after the fact.
LeaderboardEntry:   player_name, score
```

### 12.3 "Caching: active session state, current round state, leaderboard,
or recently used sequences."

**Exact structure** (`app/cache/store.py`, Redis via `redis-py`):

| Key pattern | Value | TTL | Written by | Read by |
|---|---|---|---|---|
| `session:{session_id}` | JSON-serialized `SessionState.model_dump()` | 1800s (30 min) | `set_active_session()` — after every `start_session`/`submit_answer`/`end_session` | `get_active_session()` — first thing `GameService.get_state()` tries |
| `leaderboard` | JSON-serialized `list[LeaderboardEntry.model_dump()]` | 60s | `set_leaderboard()` | `get_leaderboard()` — first thing `GameService.leaderboard()` tries |

**Access pattern — cache-aside, not write-through for reads:**
1. Read path: try Redis `GET` → on hit, deserialize and return immediately
   (no Postgres touched at all). On miss (including a Redis *outage*, per
   §5's resilience fix — treated identically to a miss), query Postgres,
   then `SETEX` to warm the cache for the next read.
2. Write path: Postgres is written first (it's the source of truth, inside
   a transaction with the constraint from §12.2 enforcing correctness), only
   *then* is the cache updated/invalidated — `set_active_session()` after a
   successful commit, `invalidate_leaderboard()` whenever a score could have
   changed (a round answered or a session ended).

**Why these two specific keys, not "recently used sequences"**: the brief
offers several options ("active session state, current round state,
leaderboard, or recently used sequences") — this project picked active
session state (the hottest read: polled every 1.5s per connected player,
plus read on every voice turn) and the leaderboard (a classic
read-heavy/write-light cache candidate) as the two with the clearest
performance payoff. "Recently used sequences" wouldn't have needed caching
here since `engine.generate_sequence()` is a pure, in-memory
`random.sample()` call — cheaper than a cache round-trip would be.

---

## 13. Technical Requirements checklist — what we did for each

The brief's "Technical Requirements" section, in its exact order, mapped to
concrete code. Several of these are covered in more depth elsewhere in this
doc — this section is the quick-reference version; follow the cross-refs
for the full story.

| # | Requirement | What we did | Deeper coverage |
|---|---|---|---|
| 1 | Use Pipecat as the core framework | Real `pipecat-ai==0.0.108` pipeline in `app/voice/bot.py`: `Pipeline([transport.input(), stt, game, llm, tts, transport.output()])`. Not a wrapper around a different voice stack — Pipecat's actual `FrameProcessor`/frame model is what the whole system is built on. | §2, §3.2 |
| 2 | Proper turn-taking | `MemoryGameProcessor.process_frame()` doesn't evaluate on either "VAD says user stopped" or "final transcript arrived" alone — it waits for **both**, whichever comes last. Necessary because Deepgram's final transcript and VAD's stop signal race each other and can arrive in either order. | §3.2, §9.2 |
| 3 | Handle interruptions cleanly, demonstrate in video | `broadcast_interruption()` called manually from `MemoryGameProcessor` when VAD detects speech while `self._bot_speaking` is true — this pipeline's transport doesn't emit `StartInterruptionFrame` automatically without a `turn_analyzer`/`LLMUserAggregator`, which was found by tracing the framework source, not assumed. `docs/video-walkthrough-script.md` has the exact on-camera narration for demonstrating this. | §3.2, §9.6, §10 |
| 4 | Engaging, human-like game-host behavior | `HOST_SYSTEM` persona prompt (`app/voice/game_processor.py`) + `_banter()` calls at every juncture — greeting, correct-answer reaction, game-over — via Groq, deliberately kept to "ONE short, energetic sentence" for low latency. Never used to decide correctness (§8.1). | §3.2, §8.1 |
| 5 | Persist session/round/response/score in a database | 3-table Postgres schema (`app/db/models.py`): `game_sessions` → `rounds` → `responses`, with cascade deletes and the `UniqueConstraint` that backs requirement #8. | §12.1, §12.2 |
| 6 | Expose backend APIs for session/score data | 5 REST endpoints (`app/api/routes.py`), all thin wrappers around the same `GameService` the voice pipeline calls — no separate "API version" of the game logic. | §3.1, §12.2 |
| 7 | Caching for ≥1 meaningful backend flow | Redis-backed active-session state (30 min TTL) and leaderboard (60s TTL) — two keys, cache-aside pattern, resilient to Redis outages (§5). | §4, §12.3 |
| 8 | Avoid double-scoring the same response | Two layers, not one: an application-level idempotency check (`rnd.status != "PENDING"`) for the common sequential-retry case, **and** a DB-level `UniqueConstraint("round_id")` plus `IntegrityError` recovery for the actual concurrent-race case a check-then-act condition can't prevent on its own. This is a real bug that was found and fixed, not a feature written correctly the first time. | §6.1, §10 |
| 9 | Word/card list hardcoded or seeded | 26-word curated pool (`app/game/words.py`), seeded rather than generated — chosen to be phonetically distinct for STT accuracy. Its one implicit invariant (never overlapping `engine.FILLER`) is enforced by a dedicated test, not just a comment. | §10 |

**The one-sentence framing for the video, if asked "which of these was hardest":**
*"Turn-taking and interruptions, #2 and #3 — because they're the two places where Pipecat's default behavior quietly didn't apply to a pipeline this minimal, and the only way to find that was reading the framework's own source, not its docs."*

---

## 14. Complete API reference — every endpoint, with real curl examples

All five endpoints, base URL `http://localhost:8000`. Every request/response
below is a **real captured run** against a live server backed by actual
Postgres and Redis — not fabricated example JSON — so the shapes are exactly
what you'll get.

### 14.1 `POST /api/sessions` — start a session

Calls `GameService.start_session()`: creates a `GameSession` row (status
`ACTIVE`, score 0, round 1), generates round 1's sequence via
`engine.generate_sequence()`, creates its `Round` row, commits, then warms
the Redis cache (`store.set_active_session`).

```bash
curl -s -X POST http://localhost:8000/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"player_name": "DocsDemo"}'
```
```json
{
  "session_id": "f0536412-0b05-4173-88da-e267bfc34d9d",
  "player_name": "DocsDemo",
  "status": "ACTIVE",
  "score": 0,
  "current_round": 1,
  "round_id": "e453e87e-b8b2-40c6-b711-ccb07c401a3c",
  "sequence_length": 3,
  "last_expected": null,
  "last_heard": null,
  "last_correct": null
}
```
Note what's *not* here: the actual sequence. Only its length is ever exposed.

### 14.2 `GET /api/sessions/{session_id}` — read state

Calls `GameService.get_state()` — **cache-first**: tries Redis
(`get_active_session`) before ever touching Postgres.

```bash
curl -s http://localhost:8000/api/sessions/f0536412-0b05-4173-88da-e267bfc34d9d
```
```json
{
  "session_id": "f0536412-0b05-4173-88da-e267bfc34d9d",
  "player_name": "DocsDemo",
  "status": "ACTIVE",
  "score": 0,
  "current_round": 1,
  "round_id": "e453e87e-b8b2-40c6-b711-ccb07c401a3c",
  "sequence_length": 3,
  "last_expected": null,
  "last_heard": null,
  "last_correct": null
}
```

**404 for an unknown session** (real captured response):
```bash
curl -s -w "\nHTTP %{http_code}\n" http://localhost:8000/api/sessions/does-not-exist
```
```
{"detail":"session not found"}
HTTP 404
```

### 14.3 `POST /api/sessions/{session_id}/answer` — submit an answer

`app/api/routes.py` first calls `GameService.get_state()` to find the
**current** `round_id`, then `GameService.submit_answer()`. Important
detail this reveals: **this endpoint always answers whatever the current
round is** — there's no way to target an old, already-resolved round
through this route. (The true "same round_id twice" idempotency guarantee
is a `GameService`-level contract, used directly by the voice pipeline —
demonstrated in §14.6 below, not through this endpoint.)

```bash
curl -s -X POST http://localhost:8000/api/sessions/f0536412-0b05-4173-88da-e267bfc34d9d/answer \
  -H "Content-Type: application/json" \
  -d '{"transcript": "monkey helicopter candle"}'
```
```json
{
  "session_id": "f0536412-0b05-4173-88da-e267bfc34d9d",
  "round_number": 1,
  "is_correct": true,
  "points_awarded": 30,
  "total_score": 30,
  "status": "ACTIVE",
  "expected": ["monkey", "helicopter", "candle"],
  "heard": ["monkey", "helicopter", "candle"]
}
```
Note `expected`/`heard` **are** the real words here — but only ever for the
round just answered, revealed after the fact (§12.2).

**409 when there's no active round to answer** (session already ended):
```bash
curl -s -X POST http://localhost:8000/api/sessions/0834bc37-cec4-43f3-9fcf-a23cbaf1d9e3/answer \
  -H "Content-Type: application/json" -d '{"transcript": "anything"}' \
  -w "\nHTTP %{http_code}\n"
```
```
{"detail":"no active round"}
HTTP 409
```

### 14.4 `POST /api/sessions/{session_id}/end` — end a session

Calls `GameService.end_session()`: marks the session `ENDED` (only if it
was still `ACTIVE` — idempotent if called twice), drops its Redis key, and
invalidates the leaderboard cache (score data just became final).

```bash
curl -s -X POST http://localhost:8000/api/sessions/0834bc37-cec4-43f3-9fcf-a23cbaf1d9e3/end
```
```json
{
  "session_id": "0834bc37-cec4-43f3-9fcf-a23cbaf1d9e3",
  "player_name": "IdempotencyDemo",
  "status": "ENDED",
  "score": 30,
  "current_round": 2,
  "round_id": null,
  "sequence_length": null,
  "last_expected": ["candle", "castle", "rocket"],
  "last_heard": ["candle", "castle", "rocket"],
  "last_correct": true
}
```

### 14.5 `GET /api/leaderboard?limit=10` — top scores

Calls `GameService.leaderboard()` — cache-first against Redis; on a miss,
`GROUP BY player_name, MAX(score)` so each player appears once (§10's dedup
fix), then warms the cache.

```bash
curl -s "http://localhost:8000/api/leaderboard?limit=5"
```
```json
[
  {"player_name": "chandan", "score": 120},
  {"player_name": "Player", "score": 70},
  {"player_name": "shruti", "score": 30},
  {"player_name": "xvcvx", "score": 30},
  {"player_name": "shhhhhh", "score": 30}
]
```

### 14.6 Real idempotency demo — the same round_id submitted twice

This is the `GameService`-level guarantee the voice pipeline relies on
directly (§14.3 explained why the REST `/answer` route can't demonstrate
this the same way — it always targets the current round). Captured from an
actual run:

```python
from app.db.database import SessionLocal
from app.game.service import GameService

db = SessionLocal()
svc = GameService(db)
state = svc.start_session("IdempotencyDemo")
seq = svc.get_current_sequence(state.session_id)
transcript = " ".join(seq)

first = svc.submit_answer(state.session_id, state.round_id, transcript)
again = svc.submit_answer(state.session_id, state.round_id, transcript)  # SAME round_id
```
```
first:  points_awarded=30 total_score=30
second: points_awarded=30 total_score=30   # NOT 60 — the second call returned
                                            # the stored result, it did not re-score
```

---

## 15. Complete database logic — every `GameService` method, in plain terms

**The simple version first:** `GameService` is the only code in this
project allowed to touch the database. Every method below does one clear
job. Read this section top to bottom and you understand the entire game's
backend logic — nothing important happens outside these functions.

### 15.1 `start_session(player_name)` — begin a new game

**What it does, in one sentence:** creates a new player and their first
round, and remembers that they're playing.

**Step by step:**
1. Create a `GameSession` row: `status="ACTIVE"`, `score=0`, `current_round=1`.
2. Call the private helper `_new_round()`, which asks `engine.generate_sequence(1, max_len)`
   for round 1's words (3 words — see §12.1's difficulty ramp) and creates
   the matching `Round` row (`status="PENDING"`).
3. Commit both rows to Postgres in one transaction.
4. Build a `SessionState` and write it into Redis (`store.set_active_session`)
   so the very next read doesn't have to hit Postgres at all.

### 15.2 `get_state(session_id)` — read the current state

**What it does, in one sentence:** answers "what's happening in this game
right now" — score, round, whether it's still active — as fast as possible.

**Step by step:**
1. Try Redis first (`store.get_active_session`). If found, deserialize and
   return immediately — **Postgres is never touched on a cache hit.**
2. On a miss, load the `GameSession` row from Postgres. If it doesn't
   exist, raise `KeyError` (the API layer turns this into a `404`).
3. Build the `SessionState` (score, round, and — via `_last_answered_round()`
   — the previous round's expected/heard words, if any exist yet).
4. If the session is still `ACTIVE`, write that state back into Redis
   (re-warms the cache for the next read).

### 15.3 `get_current_sequence(session_id)` — the actual words

**What it does, in one sentence:** hands the voice bot the literal words it
needs to say out loud — this is the *only* place in the whole codebase that
returns the unanswered sequence, and it's never reachable through the REST
API (see `app/api/routes.py` — no route calls it).

### 15.4 `submit_answer(session_id, round_id, transcript)` — judge an answer

**What it does, in one sentence:** decides if you got it right, and moves
the game forward or ends it — this is the single most important function
in the project.

**Step by step:**
1. Load the `GameSession` and the `Round` — if either's missing or doesn't
   belong to this session, raise `KeyError`.
2. **Idempotency check**: if this round already has a stored `Response`,
   stop here and return that stored result unchanged (`_result_from_stored`)
   — don't re-score something already scored.
3. Call `engine.evaluate(expected_sequence, transcript)` — the one and only
   place correctness is decided, 100% deterministic Python, no LLM involved.
4. Save a new `Response` row with the verdict.
5. **If correct:** add points to the session's score, advance `current_round`,
   and call `_new_round()` to generate the *next* round's sequence.
6. **If wrong:** mark the session `ENDED` and stamp `ended_at`.
7. Commit. **If that commit fails with `IntegrityError`** — meaning another
   request already answered this exact round first, a genuine race — roll
   back, re-read what actually got saved, and return *that* instead of
   crashing (§6.1 has the full story of this bug and its fix).
8. Refresh Redis: warm the session cache if still `ACTIVE`, or drop it and
   invalidate the leaderboard cache if the game just ended.

### 15.5 `end_session(session_id)` — stop early

**What it does, in one sentence:** lets a player quit mid-game, cleanly.

Only actually changes anything if the session was still `ACTIVE` (calling
this twice is a safe no-op, not an error) — marks it `ENDED`, stamps
`ended_at`, then drops the Redis cache entry and invalidates the
leaderboard (since a session ending is exactly when the leaderboard could
change).

### 15.6 `leaderboard(limit)` — the top scores

**What it does, in one sentence:** shows the best score **per player**,
not per game played.

Cache-first (§16 has the full cache story). On a miss: `GROUP BY
player_name`, take `MAX(score)` per group, order by that descending (tied
scores broken by whoever reached it first) — this is the fix from §10 that
stops the same player showing up multiple times just because they played
more than once.

### 15.7 The two helper methods worth understanding

- **`_current_round(session)`** — finds the one `Round` row matching
  `session.current_round`. Every "what's the active round" question in the
  whole service goes through this one query.
- **`_last_answered_round(session)`** — finds the most recently answered
  round (correct *or* wrong) by joining `Round` to `Response` and taking
  the highest `round_number` that has one. This is what powers the
  "expected vs. heard" feedback shown after each round.

---

## 16. Complete cache logic — every `store.py` function, in plain terms

**The simple version first:** `app/cache/store.py` is a thin wrapper around
Redis with six functions. Nothing in this file knows anything about
"games" — it just stores and retrieves JSON blobs by key, with an
expiration time. All the game-specific meaning (what a "session" is) lives
entirely in `GameService`, which is the *only* code that calls these
functions.

### 16.1 The two things that get cached, and why

| What | Redis key | Expires after | Why cache it |
|---|---|---|---|
| Active session state | `session:{id}` | 30 min | Read on **every** voice turn and every `GET /api/sessions/{id}` poll (frontend polls every 1.5s) — the hottest read path in the whole app. |
| Leaderboard | `leaderboard` | 60 sec | Read whenever anyone views the leaderboard; recomputing it means scanning/grouping every `GameSession` row, which is wasteful to do on every single view. |

### 16.2 The six functions

- **`get_active_session(session_id)`** — `GET session:{id}` from Redis,
  JSON-decode it if found, return `None` if not (a genuine miss *or* Redis
  being down — see 16.4).
- **`set_active_session(session_id, state)`** — JSON-encode `state` and
  `SETEX session:{id} 1800 <json>` — the `SETEX` is what sets the 30-minute
  expiry atomically with the write.
- **`drop_active_session(session_id)`** — `DEL session:{id}` — called when
  a session ends, since there's nothing left worth caching.
- **`get_leaderboard()` / `set_leaderboard(rows)`** — identical pattern,
  key `leaderboard`, 60-second `SETEX`.
- **`invalidate_leaderboard()`** — `DEL leaderboard` — called by
  `submit_answer()` and `end_session()` any time a score could have
  changed, so the cache never serves a leaderboard that's more than a
  moment stale relative to an actual change (as opposed to just expiring
  naturally after 60s regardless of whether anything changed).
- **`clear_all()`** — test/dev helper only, wipes just this app's own keys
  (`session:*` + `leaderboard`) via `SCAN`, deliberately never `FLUSHDB` —
  this Redis instance/database might be shared with other data.

### 16.3 The cache-first read pattern, traced through a real example

This is exactly what happens when you call `GET /api/sessions/{id}` —
captured live:

```bash
$ curl -s -X POST http://localhost:8000/api/sessions -d '{"player_name":"CacheDemo"}' ...
# session_id: a31157c9-52a2-433c-8d4e-3ed20b8cb227

$ docker compose exec redis redis-cli GET "session:a31157c9-52a2-433c-8d4e-3ed20b8cb227"
{"session_id": "a31157c9-...", "player_name": "CacheDemo", "status": "ACTIVE",
 "score": 0, "current_round": 1, "round_id": "4e9e4fe5-...", "sequence_length": 3, ...}

$ docker compose exec redis redis-cli TTL "session:a31157c9-52a2-433c-8d4e-3ed20b8cb227"
1800   # exactly 30 minutes, freshly set

$ curl -s -X POST http://localhost:8000/api/sessions/a31157c9-.../end   # end the session

$ docker compose exec redis redis-cli GET "session:a31157c9-52a2-433c-8d4e-3ed20b8cb227"
(nil)   # gone immediately — drop_active_session() ran as part of end_session()

$ docker compose exec redis redis-cli TTL "session:a31157c9-52a2-433c-8d4e-3ed20b8cb227"
-2      # -2 means "key does not exist" in Redis's own convention (-1 would mean
        # "exists but no expiry set")
```

The read side of this (`GameService.get_state`) is exactly two branches:
**cache hit → return, never touch Postgres. Cache miss → query Postgres,
then write the result into Redis so the *next* read is a hit.** That's the
entire cache-aside pattern this project uses — no more complicated than
that.

### 16.4 What happens if Redis itself is down

**The simple version:** the game still works, just a bit slower, because
every function above is wrapped in a `try/except redis.exceptions.RedisError`.

- A failed **read** (`get_active_session`, `get_leaderboard`) returns `None`
  — which is *exactly* what a normal cache miss also looks like, so
  `GameService` can't even tell the difference; it just falls back to
  Postgres, same as always.
- A failed **write** (`set_active_session`, etc.) logs a warning and does
  nothing else — there's no result to return from a cache write, so there's
  nothing to fall back to; the write is simply skipped.

This was a real gap found during development, not a feature designed this
way from the start (§5, §10 have the full before/after story) — the
original code had no error handling around Redis calls at all, so an
outage would have taken down every request that touched the cache.
