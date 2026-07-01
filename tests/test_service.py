from app.game.service import GameService
from app.cache import store


def test_start_session_creates_round_one(db):
    store.clear_all()
    svc = GameService(db)
    state = svc.start_session("Alice")
    assert state.player_name == "Alice"
    assert state.status == "ACTIVE"
    assert state.current_round == 1
    assert state.score == 0
    assert state.sequence_length == 3        # round 1 = 3 words
    assert state.round_id is not None
    # cache is warmed
    assert store.get_active_session(state.session_id) is not None


def test_get_state_reads_from_cache(db):
    store.clear_all()
    svc = GameService(db)
    started = svc.start_session("Bob")
    # mutate cache to prove get_state prefers it
    cached = store.get_active_session(started.session_id)
    cached["score"] = 999
    store.set_active_session(started.session_id, cached)
    assert svc.get_state(started.session_id).score == 999

def test_get_sequence_returns_words_for_bot(db):
    store.clear_all()
    svc = GameService(db)
    started = svc.start_session("Cara")
    seq = svc.get_current_sequence(started.session_id)
    assert len(seq) == 3
