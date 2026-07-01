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
