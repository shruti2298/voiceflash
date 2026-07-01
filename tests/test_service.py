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


def test_correct_answer_advances_and_scores(db):
    store.clear_all()
    svc = GameService(db)
    started = svc.start_session("Dee")
    seq = svc.get_current_sequence(started.session_id)
    res = svc.submit_answer(started.session_id, started.round_id, " ".join(seq))
    assert res.is_correct is True
    assert res.points_awarded == 30          # 10 * 3
    assert res.total_score == 30
    assert res.status == "ACTIVE"
    # advanced to round 2
    assert svc.get_state(started.session_id).current_round == 2

def test_wrong_answer_ends_game(db):
    store.clear_all()
    svc = GameService(db)
    started = svc.start_session("Eve")
    res = svc.submit_answer(started.session_id, started.round_id, "definitely wrong words")
    assert res.is_correct is False
    assert res.status == "ENDED"
    assert svc.get_state(started.session_id).status == "ENDED"

def test_double_submission_is_idempotent(db):
    store.clear_all()
    svc = GameService(db)
    started = svc.start_session("Foo")
    seq = svc.get_current_sequence(started.session_id)
    first = svc.submit_answer(started.session_id, started.round_id, " ".join(seq))
    # submit the SAME round again — must return the same result, not re-score
    again = svc.submit_answer(started.session_id, started.round_id, " ".join(seq))
    assert again.points_awarded == first.points_awarded
    assert again.total_score == first.total_score == 30   # not 60
