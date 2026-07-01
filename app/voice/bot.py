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

    # allow_interruptions defaults to True in this Pipecat version (0.0.108),
    # which is what enables barge-in recovery.
    task = PipelineTask(pipeline, params=PipelineParams())

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
