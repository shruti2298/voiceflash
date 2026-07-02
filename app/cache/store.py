"""Redis-backed caches. Two meaningful flows:
   - active session state: read on every voice turn + GET /state (hot path)
   - leaderboard: short-TTL cache, invalidated when a session ends

   Redis (not an in-process cache) so that multiple Uvicorn workers or
   horizontally-scaled instances all see the same active-session state and
   leaderboard, instead of each process holding its own out-of-sync copy.
"""
import json
from typing import Any, Optional

import redis as redis_lib

from app.config import settings

_ACTIVE_SESSION_TTL = 30 * 60  # 30 minutes
_LEADERBOARD_TTL = 60          # 60 seconds

_SESSION_KEY_PREFIX = "session:"
_LEADERBOARD_KEY = "leaderboard"

# Lazy connection: redis-py doesn't actually connect until the first command,
# so constructing this doesn't require Redis to be reachable yet.
_redis = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)


def _session_key(session_id: str) -> str:
    return f"{_SESSION_KEY_PREFIX}{session_id}"


def get_active_session(session_id: str) -> Optional[Any]:
    raw = _redis.get(_session_key(session_id))
    return json.loads(raw) if raw is not None else None


def set_active_session(session_id: str, state: Any) -> None:
    _redis.setex(_session_key(session_id), _ACTIVE_SESSION_TTL, json.dumps(state))


def drop_active_session(session_id: str) -> None:
    _redis.delete(_session_key(session_id))


def get_leaderboard() -> Optional[Any]:
    raw = _redis.get(_LEADERBOARD_KEY)
    return json.loads(raw) if raw is not None else None


def set_leaderboard(rows: Any) -> None:
    _redis.setex(_LEADERBOARD_KEY, _LEADERBOARD_TTL, json.dumps(rows))


def invalidate_leaderboard() -> None:
    _redis.delete(_LEADERBOARD_KEY)


def clear_all() -> None:
    """Test/dev helper: wipe only this app's namespaced keys — never FLUSHDB,
    since this Redis instance/database may be shared with other data."""
    keys = list(_redis.scan_iter(match=f"{_SESSION_KEY_PREFIX}*"))
    keys.append(_LEADERBOARD_KEY)
    if keys:
        _redis.delete(*keys)
