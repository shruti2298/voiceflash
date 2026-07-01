"""In-memory caches (cachetools). Two meaningful flows:
   - active session state: read on every voice turn + GET /state (hot path)
   - leaderboard: short-TTL cache, invalidated when a session ends
"""
from typing import Any, Optional

from cachetools import TTLCache

# up to 1000 active sessions, expire 30 min after last write
_active_sessions: TTLCache = TTLCache(maxsize=1000, ttl=30 * 60)
# single leaderboard snapshot, 60s freshness
_leaderboard: TTLCache = TTLCache(maxsize=1, ttl=60)

_LB_KEY = "leaderboard"


def get_active_session(session_id: str) -> Optional[Any]:
    return _active_sessions.get(session_id)


def set_active_session(session_id: str, state: Any) -> None:
    _active_sessions[session_id] = state


def drop_active_session(session_id: str) -> None:
    _active_sessions.pop(session_id, None)


def get_leaderboard() -> Optional[Any]:
    return _leaderboard.get(_LB_KEY)


def set_leaderboard(rows: Any) -> None:
    _leaderboard[_LB_KEY] = rows


def invalidate_leaderboard() -> None:
    _leaderboard.pop(_LB_KEY, None)


def clear_all() -> None:
    _active_sessions.clear()
    _leaderboard.clear()
