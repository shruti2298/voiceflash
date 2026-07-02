import asyncio

from pipecat.frames.frames import (
    Frame, LLMMessagesFrame, TTSSpeakFrame, TranscriptionFrame,
    VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame, StartInterruptionFrame,
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

    Turn boundaries: this pipeline has no LLMUserAggregator or turn_analyzer, so
    the transport's VAD emits VADUserStartedSpeakingFrame / VADUserStoppedSpeakingFrame
    (not the plain UserStartedSpeakingFrame/UserStoppedSpeakingFrame — those are only
    pushed by the deprecated turn-analyzer/aggregator code path in this Pipecat
    version, which this pipeline doesn't use).
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
                state = svc.get_state(self._session_id)
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

        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._buffer.clear()
            self._turn_active = True
            self._user_stopped = False

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
