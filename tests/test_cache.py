from app.cache import store


def test_active_session_roundtrip():
    store.clear_all()
    store.set_active_session("s1", {"score": 30})
    assert store.get_active_session("s1") == {"score": 30}

def test_missing_session_returns_none():
    store.clear_all()
    assert store.get_active_session("nope") is None

def test_leaderboard_cache_and_invalidate():
    store.clear_all()
    store.set_leaderboard([{"player_name": "a", "score": 50}])
    assert store.get_leaderboard()[0]["player_name"] == "a"
    store.invalidate_leaderboard()
    assert store.get_leaderboard() is None
