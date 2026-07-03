import asyncio

import pytest
from pipecat.frames.frames import InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
import app.voice.game_processor as gp_module
from app.voice.game_processor import MemoryGameProcessor


@pytest.fixture()
def sqlite_session_local(monkeypatch):
    """Point the module's SessionLocal at an isolated in-memory SQLite DB,
    mirroring tests/conftest.py's `db` fixture but for game_processor.py,
    which manages its own short-lived sessions rather than accepting an
    injected one. StaticPool is required (unlike the `db` fixture) because
    game_processor.py runs its DB calls via asyncio.to_thread — a different
    thread than the one that ran create_all() — and SQLite's default
    per-thread pooling would otherwise hand that thread a fresh, empty
    :memory: database (same issue solved for tests/test_api.py earlier)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr(gp_module, "SessionLocal", TestSession)
    return TestSession


def test_start_session_sync_resumes_known_session(sqlite_session_local):
    # Seed a session directly via the service, using the same SessionLocal
    # the processor will use.
    from app.game.service import GameService
    db = sqlite_session_local()
    started = GameService(db).start_session("Alice")
    db.close()

    processor = MemoryGameProcessor(player_name="Alice", session_id=started.session_id)
    state, seq = processor._start_session_sync()

    assert state.session_id == started.session_id
    assert len(seq) == 3

def test_start_session_sync_falls_back_when_session_id_unknown(sqlite_session_local):
    processor = MemoryGameProcessor(player_name="Ghost", session_id="does-not-exist")
    state, seq = processor._start_session_sync()

    # Must not raise, and must produce a fresh, working session instead.
    assert state.session_id != "does-not-exist"
    assert state.player_name == "Ghost"
    assert state.status == "ACTIVE"
    assert len(seq) == 3


def test_turn_timeout_scales_with_sequence_length():
    # Longer (harder) sequences must get more time to say out loud, not the
    # same flat timeout regardless of round difficulty.
    short = gp_module._turn_timeout_for(3)
    long = gp_module._turn_timeout_for(6)
    assert long > short
    assert long - short == pytest.approx(gp_module._TURN_TIMEOUT_PER_WORD_SECS * 3)


def test_turn_watchdog_force_finishes_when_stop_signal_never_arrives(sqlite_session_local, monkeypatch):
    monkeypatch.setattr(gp_module, "_TURN_TIMEOUT_BASE_SECS", 0.05)
    monkeypatch.setattr(gp_module, "_TURN_TIMEOUT_PER_WORD_SECS", 0.0)

    processor = MemoryGameProcessor(player_name="Slowpoke")
    spoken = []

    async def fake_speak(text):
        spoken.append(("speak", text))

    async def fake_banter(instruction):
        spoken.append(("banter", instruction))

    processor._speak = fake_speak
    processor._banter = fake_banter

    async def scenario():
        await processor._start_game()
        spoken.clear()

        # Simulate VAD detecting speech that never gets a stop signal, and
        # something was actually heard before the timeout fires.
        processor._buffer = ["hello"]
        processor._turn_active = True
        task = asyncio.create_task(processor._turn_timeout_watchdog())
        processor._turn_timeout_task = task
        await task  # wait for the forced finish (including its DB work) to complete

    asyncio.run(scenario())

    assert processor._turn_active is False  # turn was resolved, not left hanging
    assert spoken  # the forced finish evaluated the answer and spoke a reaction

def test_turn_watchdog_does_not_fire_if_turn_finishes_normally(sqlite_session_local, monkeypatch):
    monkeypatch.setattr(gp_module, "_TURN_TIMEOUT_BASE_SECS", 0.05)
    monkeypatch.setattr(gp_module, "_TURN_TIMEOUT_PER_WORD_SECS", 0.0)

    processor = MemoryGameProcessor(player_name="Quickdraw")
    processor._speak = lambda text: asyncio.sleep(0)
    processor._banter = lambda instruction: asyncio.sleep(0)

    async def scenario():
        await processor._start_game()
        processor._turn_active = True
        task = asyncio.create_task(processor._turn_timeout_watchdog())
        processor._turn_timeout_task = task
        processor._reset_turn()  # normal completion path cancels the watchdog
        await asyncio.sleep(0.2)  # long enough that the watchdog would've fired if not cancelled
        return task

    task = asyncio.run(scenario())
    assert task.cancelled()


def test_process_frame_resets_turn_on_any_interruption_frame():
    # broadcast_interruption() (used by our own barge-in path) creates a plain
    # InterruptionFrame, not a StartInterruptionFrame — so process_frame must
    # react to the base InterruptionFrame class, not just its subclass, or
    # this reset never actually fires for any interruption this pipeline
    # produces.
    processor = MemoryGameProcessor(player_name="Interrupted")
    processor._turn_active = True
    processor._buffer = ["some", "words"]

    asyncio.run(processor.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM))

    assert processor._turn_active is False
    assert processor._buffer == []
