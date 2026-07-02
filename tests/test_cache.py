import redis as redis_lib

from app.cache import store


class _BoomRedis:
    """Stand-in for a Redis client whose every call fails, simulating an outage."""
    def get(self, *a, **kw):
        raise redis_lib.exceptions.ConnectionError("boom")

    def setex(self, *a, **kw):
        raise redis_lib.exceptions.ConnectionError("boom")

    def delete(self, *a, **kw):
        raise redis_lib.exceptions.ConnectionError("boom")

    def scan_iter(self, *a, **kw):
        raise redis_lib.exceptions.ConnectionError("boom")


def test_get_active_session_returns_none_on_redis_outage(monkeypatch):
    monkeypatch.setattr(store, "_redis", _BoomRedis())
    assert store.get_active_session("s1") is None

def test_set_active_session_does_not_raise_on_redis_outage(monkeypatch):
    monkeypatch.setattr(store, "_redis", _BoomRedis())
    store.set_active_session("s1", {"score": 1})  # must not raise

def test_drop_active_session_does_not_raise_on_redis_outage(monkeypatch):
    monkeypatch.setattr(store, "_redis", _BoomRedis())
    store.drop_active_session("s1")  # must not raise

def test_get_leaderboard_returns_none_on_redis_outage(monkeypatch):
    monkeypatch.setattr(store, "_redis", _BoomRedis())
    assert store.get_leaderboard() is None

def test_set_leaderboard_does_not_raise_on_redis_outage(monkeypatch):
    monkeypatch.setattr(store, "_redis", _BoomRedis())
    store.set_leaderboard([{"player_name": "a", "score": 1}])  # must not raise

def test_invalidate_leaderboard_does_not_raise_on_redis_outage(monkeypatch):
    monkeypatch.setattr(store, "_redis", _BoomRedis())
    store.invalidate_leaderboard()  # must not raise

def test_clear_all_does_not_raise_on_redis_outage(monkeypatch):
    monkeypatch.setattr(store, "_redis", _BoomRedis())
    store.clear_all()  # must not raise


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
