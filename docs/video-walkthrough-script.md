# Video Walkthrough Script — Full 10–15 Minute Recording

The assignment wants one screen-share video covering three things, in this
order: **Demo**, **Interruption handling**, **Code walkthrough**. This is a
practical script for all three — keep this file open in a second window
while recording.

## Time budget (10–15 min total)

| Part | Suggested time | What it proves |
|---|---|---|
| 1. Demo | 3–4 min | The whole thing actually works, end to end |
| 2. Interruption handling | 2–3 min | The one behavior the brief explicitly requires you to *demonstrate*, not just describe |
| 3. Code walkthrough | 6–8 min | You understand what you built, not just that it runs |

Do a **dry run once without recording** first — mainly to check your mic
picks up your voice clearly enough for Deepgram to actually transcribe you
on camera. A demo where the bot mishears you repeatedly is a bad first
impression and is usually a mic-gain problem, not a bug.

---

# Part 1 — Demo (3–4 min)

**Goal: show a complete game, start to finish, including both outcomes.**
Don't narrate the code yet — that's Part 3. Just play the game naturally
and talk about what's happening on screen.

### What to actually do, in order

1. **Open the app**, point out the "How to Play" panel is right there for
   anyone unfamiliar — then collapse it or scroll past, don't dwell on it.
2. **Type a real name**, click **Start voice game**, allow the mic prompt
   when it appears. Say out loud: *"Name's required now — you can't start
   without one."*
3. **Let the bot greet you and speak round 1's sequence** (3 words). Don't
   talk over it yet — save that for Part 2.
4. **Repeat it back correctly.** Point at the screen when the score bumps
   and the confetti fires: *"That's the score updating live — it's polling
   the backend every 1.5 seconds."*
5. **Play at least one more round correctly** so the sequence visibly grows
   (4 words) — call out the difficulty ramp: *"Every correct round, the
   sequence gets one word longer."*
6. **Deliberately get one wrong** — say random unrelated words. Show the
   "Game Over" screen with the final score, and the **"Expected vs. You
   said"** panel: *"It shows exactly what it expected versus what it heard
   — no hidden judgment, you can see precisely why it was marked wrong."*
7. **Show the leaderboard** updating with your finished score.
8. *(Optional, if time allows)* Click **Play Again**, or refresh the page
   mid-game on a *different* attempt to show the "Welcome back" resume
   banner — a nice, non-obvious touch worth 15 seconds if you have them.

### One sentence to say somewhere in this section

> "Everything you're seeing scored right now is deterministic code judging
> me — not the AI model. I'll show exactly where that happens in the code
> walkthrough."

---

# Part 2 — Interruption handling (2–3 min)

**Goal: prove barge-in works, on camera, not just claim it does.** This is
the one thing the brief explicitly says to *demonstrate*, so don't rush it.

### What to actually do, in order

1. Start a fresh round (or continue from Part 1) and **wait for the bot to
   start speaking** — either its greeting or a "Repeat after me" line.
2. **While it's still mid-sentence, start talking over it** — say something
   clearly, like the actual answer.
3. **Point out on screen, in real time**: the bot's audio should cut off
   almost immediately, not finish its sentence first.
4. **Let it finish processing your answer normally** — show that the game
   continues correctly afterward (it correctly scored what you said, it
   didn't get confused or double-count anything).
5. *(Strongly recommended)* **Have your server terminal visible** in a
   corner or a quick cut-to, and point at the log lines that appear the
   instant you interrupt — this proves the interruption is a real,
   server-side event, not just your microphone muting the bot's audio
   client-side.

### What to say while doing it

> "Watch — I'm going to start talking while it's still mid-sentence."
> *(interrupt)*
> "It stopped immediately instead of finishing its line and then listening
> — that's not a default Pipecat gives you for free in this setup, I had to
> wire that up myself, and I'll show you exactly where in a minute."

### If it doesn't work cleanly on the first take

Don't panic-cut the recording. Say so honestly and try again — "let me
try that again, I want to make sure I catch it clean" is a completely
normal thing to say on a technical demo video and reads better than a
jump-cut. If it's still inconsistent, it's worth knowing *why* before
recording (see `ARCH.md` §3.2 / §9.6 for exactly how barge-in is detected)
rather than hoping a retake fixes it.

---

# Part 3 — Code walkthrough (6–8 min)

**Goal: prove you understand what you built, file by file.** ~60–90
seconds per section below. Don't read this verbatim word-for-word — say it
in your own voice, but hit the same points in the same order.

## 0. One-sentence framing (say this before opening any file)

> "The whole system has one rule: the LLM never decides if you're right —
> that's always deterministic code. Everything I show you supports that
> one decision."

This sentence is your anchor. If you get lost mid-walkthrough, come back to
it — it's also the answer to most "why did you do X" follow-up questions.

---

## 3.1 Pipecat pipeline setup — `app/voice/bot.py`

**Open this file. Point at the `Pipeline([...])` list first, before anything else.**

> "This is the actual voice pipeline. It's a straight line: transport input,
> Deepgram speech-to-text, my custom processor, Groq for the LLM, Deepgram
> text-to-speech, transport output. Audio comes in the left, comes out the
> right, and my code sits in the middle deciding what happens."

**Then point at the comment above the `Pipeline([...])` call** (the one
explaining LLM ordering) and say:

> "The order here matters for one reason: my processor can emit two
> different kinds of frames. A `TTSSpeakFrame` — that's the exact word
> sequence, spoken word-for-word — skips the LLM stage completely, it just
> passes through untouched. An `LLMMessagesFrame` — that's host banter, like
> greetings and reactions — actually goes through Groq. So the LLM
> physically cannot touch the words being tested, because I never send them
> to it."

**Point at `default_transport_params()` at the bottom:**

> "This sets up the WebRTC transport — mic in, mic out, and Silero VAD for
> detecting when someone's talking."

---

## 3.2 Custom frame processor — `app/voice/game_processor.py`

This is your biggest, most important file. Don't try to cover every line —
hit these four things in order.

**1. Open the class docstring at the top.** Read the barge-in/turn-taking
paragraphs almost verbatim — they're already written as talking points:

> "This pipeline deliberately doesn't use Pipecat's built-in conversation
> management — no turn analyzer, no LLM aggregator, because that pulls in a
> lot of complexity I didn't need. But that means two things I had to
> handle myself, which is actually the most interesting engineering story
> in this project."

**2. Scroll to `process_frame()` — the turn-taking part.**

> "Deepgram's final transcript and the 'user stopped speaking' signal from
> VAD can arrive in either order — sometimes the transcript shows up after
> the stop signal. So I don't evaluate on either one alone — I wait until I
> have both, whichever arrives last. If I only listened for the stop
> signal, I'd sometimes evaluate an empty buffer."

**3. Scroll to the `BotStartedSpeakingFrame`/interruption handling** — this
is the exact code behind what you just demonstrated in Part 2:

> "This one I actually had to debug into existence — the framework doesn't
> emit an interruption signal on its own in this configuration. So I track
> whether the bot is currently speaking myself, and when I detect the user
> talking over it, I call `broadcast_interruption()` directly — that's a
> real framework method, just not one that gets triggered automatically
> here. That's what actually stops the bot's audio mid-sentence, which is
> exactly what you just saw."

*(If asked "how did you find that" in a follow-up: "I added temporary debug
logging, saw transcripts arriving correctly but the bot still not
reacting, and traced it into the transport's source code to find the exact
frame classes it was actually emitting.")*

**4. Briefly mention the turn-timeout watchdog** (`_turn_timeout_watchdog`):

> "One more thing: if VAD's stop signal never arrives — noisy background,
> say — the turn used to just wait forever. There's now a watchdog, scaled
> to how long the sequence is, that forces a decision instead of leaving
> the player stuck in silence on the harder, longer rounds."

---

## 3.3 Database model — `app/db/models.py`

**Open the file, scroll through all three classes once, then go back to `Response`.**

> "Three tables: a session, its rounds, and the responses to those rounds.
> Standard stuff — but there's one deliberate detail."

**Point at the `UniqueConstraint("round_id", ...)` line on `Response`.**

> "This is the actual guarantee against double-scoring. My application code
> also checks 'has this round already been answered' before scoring — but
> that check alone has a race condition if two requests land at almost the
> same time. This constraint is what makes it physically impossible for the
> database to ever hold two scored answers for the same round, no matter
> what the application code does."

*(Good place to mention, briefly, if time allows: "I actually found and
fixed that race condition — two near-simultaneous submissions could both
pass the check before either committed. The database constraint caught it,
but my code wasn't handling the resulting error gracefully — now it is.")*

---

## 3.4 APIs — `app/api/routes.py`

**Open the file — it's short, scroll through all five routes at once.**

> "Five endpoints: start a session, get its state, submit an answer, end a
> session, and the leaderboard. Nothing exotic — but the important part
> isn't visible in this file. It's what's *not* here."

**Point at any route body — e.g., `submit_answer`.**

> "Every one of these is a thin wrapper. All the actual logic — validating
> the answer, scoring, persisting, caching — lives in one place,
> `GameService`. The API layer and the voice pipeline both call the exact
> same service function. There's no separate 'API version' of the game
> logic and 'voice version' — one source of truth."

*(If you want to show that "one source of truth" claim, briefly flip to
`app/game/service.py` and point at `submit_answer` — mention it's the same
function `game_processor.py`'s `_submit_sync` calls.)*

---

## 3.5 Caching — `app/cache/store.py`

**Open the file — point at the module docstring first.**

> "Two things get cached: active session state, and the leaderboard. Both
> in Redis, not just an in-memory dictionary — and that distinction
> actually mattered. Early on I had this as an in-process cache, and it
> would've silently broken the moment I ran more than one server process,
> because each process would've had its own, disagreeing cache. Redis makes
> every process share the same view."

**Point at any of the `try/except redis_lib.exceptions.RedisError` blocks.**

> "And every single cache call is guarded like this — if Redis is down, a
> read just falls back to Postgres as a cache miss, and a write is a
> no-op with a warning logged. Redis is an optimization here, never a hard
> dependency — the game still works correctly, just slower, if the cache
> disappears entirely."

---

## 3.6 Frontend-backend communication — `static/app.js`

**Open the file, scroll to `startGame()`.**

> "The frontend talks to the backend two completely different ways at the
> same time. First, a normal REST call — `POST /api/sessions` — creates the
> session and gets back a session ID."

**Scroll down to the WebRTC offer/answer block.**

> "Then it opens a WebRTC connection directly to the voice pipeline — grabs
> the mic, builds an SDP offer, and posts that to `/rtc/offer` along with
> the *same* session ID from the REST call. That's the important part: the
> voice pipeline resumes that exact session instead of starting a second,
> disconnected one — otherwise the UI would be polling one session while
> the bot plays a completely different one, and you'd never see your score
> update."

**Scroll to `refreshState()` / the `setInterval` polling.**

> "And then it's just polling — every 1.5 seconds, `GET /api/sessions/{id}`,
> render whatever comes back. Simple, not the most efficient at scale, but
> correct and easy to reason about for this scope."

---

## Closing line (say this last, ties everything back to §0)

> "So: two entry points — REST and voice — one shared game service, one
> deterministic engine underneath both, and a cache, an interruption
> handler, and a turn-timeout that all had real bugs I found and fixed
> along the way, not just theoretical concerns."

---

## Quick reference — if you get a follow-up question mid-recording

| If asked about... | Say this, then move on |
|---|---|
| Why Postgres not NoSQL | "The data's inherently relational — session → rounds → responses — and I needed a real foreign-key/unique-constraint guarantee, not just app-level checks." |
| Why not just use the LLM to judge answers | "That was a hard requirement — validation can't depend on an LLM prompt. It's also just more reliable: deterministic string comparison never hallucinates." |
| What happens if Deepgram/Groq goes down | "STT/TTS outage breaks the voice experience — no fallback provider today. Groq outage only affects the host's personality lines, never scoring, since that's fully decoupled." |
| Scaling to more users | Point at the Redis section again — stateless REST layer, shared cache, Postgres as source of truth — "any of these could run as multiple processes with no code changes." |
| What happens if I refresh mid-game | Point at the "Welcome back" banner if you showed it in Part 1 — "the session ID is saved client-side and the backend can resume it, since the voice pipeline already knows how to pick up an existing session instead of starting a new one." |

If you want the deeper version of any of these answers, `ARCH.md` in the
repo root has the full writeup — this script is deliberately the *short*
version for on-camera pacing.
