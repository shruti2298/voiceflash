import asyncio

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, Frame, InterruptionFrame,
    LLMMessagesFrame, TTSSpeakFrame, TranscriptionFrame,
    VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
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

# If VAD never signals the user stopped talking (e.g. noisy audio prevents
# clean silence detection), force the turn to resolve instead of waiting
# forever — see MemoryGameProcessor._turn_timeout_watchdog. Scaled by the
# sequence length so harder (longer) rounds get proportionally more time:
# saying 6-8 words obviously takes longer than saying 3, and a flat timeout
# would cut off a player who's still legitimately mid-answer on later rounds.
_TURN_TIMEOUT_BASE_SECS = 6.0
_TURN_TIMEOUT_PER_WORD_SECS = 2.5


def _turn_timeout_for(sequence_length: int) -> float:
    return _TURN_TIMEOUT_BASE_SECS + _TURN_TIMEOUT_PER_WORD_SECS * max(sequence_length, 1)


class MemoryGameProcessor(FrameProcessor):
    """Drives the memory game over voice. Game logic is delegated to GameService;
    this class only handles turn-taking, speaking, and interruption recovery.

    Two speech channels:
      * _speak()  -> TTSSpeakFrame  : deterministic, spoken verbatim (the sequence)
      * _banter() -> LLMMessagesFrame: Groq generates host personality lines

    Turn boundaries: this pipeline has no LLMUserAggregator or turn_analyzer, so
    the transport's VAD emits VADUserStartedSpeakingFrame / VADUserStoppedSpeakingFrame
    (not the plain UserStartedSpeakingFrame/UserStoppedSpeakingFrame — those are only
    pushed by the deprecated turn-analyzer/aggregator code path in this Pipecat
    version, which this pipeline doesn't use).

    Barge-in: for the same reason, this pipeline's transport never emits
    StartInterruptionFrame on its own — that only happens via the deprecated
    turn-analyzer path or an LLMUserAggregator, neither of which we use. So
    this processor detects the barge-in itself (VAD fires while the bot is
    speaking) and calls broadcast_interruption(), which is what actually stops
    the in-flight TTS/audio output.
    """

    def __init__(self, player_name: str, session_id: str | None = None):
        super().__init__()
        self._player_name = player_name
        # If a session_id is provided, it was already created via the REST
        # API (POST /api/sessions) — reuse it instead of starting a second,
        # disconnected session that the frontend's polling would never see.
        self._session_id: str | None = session_id
        self._round_id: str | None = None
        self._buffer: list[str] = []
        self._turn_active = False
        self._user_stopped = False
        self._bot_speaking = False
        self._turn_timeout_task: asyncio.Task | None = None
        self._current_sequence_length = 0
        # Set right before speaking a sequence the player must repeat back,
        # and consumed by the next BotStoppedSpeakingFrame — see
        # _start_listening() and its call sites for why this can't just be
        # "listen whenever the bot stops talking" (banter/game-over lines
        # also trigger BotStoppedSpeakingFrame, and don't expect an answer).
        self._expecting_answer = False

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
            if self._session_id:
                # Resume the session the REST API already created, rather than
                # starting a brand-new one the frontend never learns about.
                try:
                    state = svc.get_state(self._session_id)
                except KeyError:
                    # The session_id the client sent doesn't exist (stale,
                    # tampered with, or a race we don't otherwise expect).
                    # Don't let that kill the call — fall back to a fresh
                    # session so the player still gets a working game.
                    logger.warning(
                        f"Unknown session_id {self._session_id!r} from voice client; "
                        f"starting a new session for {self._player_name!r} instead"
                    )
                    state = svc.start_session(self._player_name)
            else:
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
        self._current_sequence_length = len(seq)
        await self._banter(
            f"Greet the player named {self._player_name} and announce we're starting "
            f"round {state.current_round} of Memory Card."
        )
        self._expecting_answer = True
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
        self._current_sequence_length = len(seq)
        await self._banter(
            f"Correct! The player earned {result.points_awarded} points for a total of "
            f"{result.total_score}. React with excitement and tell them round "
            f"{state.current_round} is next."
        )
        self._expecting_answer = True
        await self._speak(self._say_sequence(seq))

    # ---- turn detection -------------------------------------------------
    def _cancel_turn_timeout(self):
        task = self._turn_timeout_task
        # Guard against the watchdog cancelling itself: when the timeout
        # fires and calls _finish_turn() -> _reset_turn(), this runs from
        # inside the watchdog's own task. Cancelling "yourself" mid-await
        # would raise CancelledError out of the very turn it just resolved.
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._turn_timeout_task = None

    def _reset_turn(self):
        self._cancel_turn_timeout()
        self._buffer.clear()
        self._turn_active = False
        self._user_stopped = False

    def _start_listening(self):
        """Begin (or restart) the listening window and its timeout watchdog.

        Called from two places: the moment the bot finishes speaking a
        sequence (so the timeout runs even if the player never makes a
        sound at all — see the BotStoppedSpeakingFrame branch below), and
        again once VAD actually detects speech (giving a fresh full window
        from when the player genuinely starts answering).
        """
        self._buffer.clear()
        self._turn_active = True
        self._user_stopped = False
        self._cancel_turn_timeout()
        self._turn_timeout_task = asyncio.create_task(self._turn_timeout_watchdog())

    async def _finish_turn(self):
        """Fires once per turn: normally once we have both a stop signal and a
        transcript, or forced by the timeout watchdog with no transcript at all
        if the player never said anything. Either way the round must resolve —
        an empty transcript still goes through _handle_user_turn so it's
        evaluated (and correctly scored wrong) and the session moves on or
        ends, instead of silently resetting and leaving the game hanging
        forever waiting for an answer that already timed out.
        """
        if not self._turn_active:
            return
        transcript = " ".join(self._buffer).strip()
        self._reset_turn()
        if self._session_id:
            await self._handle_user_turn(transcript)

    async def _turn_timeout_watchdog(self):
        """Force-resolves a turn if VAD never signals the user stopped talking.

        Without this, noisy audio that prevents clean silence detection would
        leave the round waiting forever — the player's answer would sit in
        the buffer, never evaluated, with no feedback from the bot at all.
        The timeout scales with the current round's sequence length (see
        _turn_timeout_for) so longer, harder sequences get proportionally
        more time to actually say out loud before being force-finished.
        """
        try:
            await asyncio.sleep(_turn_timeout_for(self._current_sequence_length))
        except asyncio.CancelledError:
            return
        if self._turn_active:
            await self._finish_turn()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            # barge-in: drop the in-progress utterance and current turn state.
            # Checking the base InterruptionFrame here (not the StartInterruptionFrame
            # subclass) matters: broadcast_interruption() — what our own barge-in
            # path below calls — creates a plain InterruptionFrame, which would
            # never satisfy an isinstance check against the narrower subclass.
            self._reset_turn()

        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True

        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            if self._expecting_answer:
                # The bot just finished speaking the sequence the player must
                # repeat — start the listening window (and its timeout) now,
                # not only once/if VAD detects the player actually talking.
                # Without this, a player who never makes any sound at all
                # left the game stuck on "Listening..." forever, since the
                # watchdog used to only ever get created below.
                self._expecting_answer = False
                self._start_listening()

        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if self._bot_speaking:
                # True barge-in: the transport doesn't emit StartInterruptionFrame
                # on its own in this pipeline (see class docstring), so we trigger
                # it ourselves — this is what actually stops the bot's audio.
                # Colored red purely so this line is easy to spot/highlight in a
                # terminal recording — it's still a normal INFO-level event, not
                # an error; logger.opt(colors=True) is loguru's own markup for
                # forcing a color without changing the log level's semantics.
                logger.opt(colors=True).info(
                    f"<red><bold>Barge-in detected</bold> for session {self._session_id!r} — "
                    "user started speaking while the bot was mid-speech; "
                    "broadcasting interruption to stop TTS/audio output</red>"
                )
                await self.broadcast_interruption()
            # Restart the clock now that real speech has actually begun,
            # giving the player the full window from this point rather than
            # from whenever the bot happened to stop talking.
            self._start_listening()

        elif isinstance(frame, TranscriptionFrame) and self._turn_active:
            self._buffer.append(frame.text)
            # transcript may arrive AFTER the stop signal — finish here if so
            if self._user_stopped:
                await self._finish_turn()

        elif isinstance(frame, VADUserStoppedSpeakingFrame) and self._turn_active:
            self._user_stopped = True
            # if the final transcript already arrived, finish now;
            # otherwise wait for it in the TranscriptionFrame branch above
            if self._buffer:
                await self._finish_turn()

        await self.push_frame(frame, direction)
