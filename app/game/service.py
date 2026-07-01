from typing import List, Optional

from sqlalchemy.orm import Session

from app.cache import store
from app.config import settings
from app.db import models
from app.game import engine
from app.schemas import AnswerResult, LeaderboardEntry, SessionState


class GameService:
    def __init__(self, db: Session):
        self.db = db

    # ---- helpers -------------------------------------------------------
    def _current_round(self, session: models.GameSession) -> Optional[models.Round]:
        return (
            self.db.query(models.Round)
            .filter(models.Round.session_id == session.id,
                    models.Round.round_number == session.current_round)
            .one_or_none()
        )

    def _state(self, session: models.GameSession) -> SessionState:
        rnd = self._current_round(session) if session.status == "ACTIVE" else None
        state = SessionState(
            session_id=session.id,
            player_name=session.player_name,
            status=session.status,
            score=session.score,
            current_round=session.current_round,
            round_id=rnd.id if rnd else None,
            sequence_length=len(rnd.sequence) if rnd else None,
        )
        return state

    def _new_round(self, session: models.GameSession) -> models.Round:
        seq = engine.generate_sequence(session.current_round, settings.max_sequence_length)
        rnd = models.Round(session_id=session.id, round_number=session.current_round,
                           sequence=seq, status="PENDING")
        self.db.add(rnd)
        self.db.flush()
        return rnd

    # ---- public API ----------------------------------------------------
    def start_session(self, player_name: str) -> SessionState:
        session = models.GameSession(player_name=player_name, status="ACTIVE",
                                     score=0, current_round=1)
        self.db.add(session)
        self.db.flush()
        self._new_round(session)
        self.db.commit()
        state = self._state(session)
        store.set_active_session(session.id, state.model_dump())
        return state

    def get_state(self, session_id: str) -> SessionState:
        cached = store.get_active_session(session_id)
        if cached is not None:
            return SessionState(**cached)
        session = self.db.get(models.GameSession, session_id)
        if session is None:
            raise KeyError(session_id)
        state = self._state(session)
        if session.status == "ACTIVE":
            store.set_active_session(session_id, state.model_dump())
        return state

    def get_current_sequence(self, session_id: str) -> List[str]:
        """Used by the voice bot only — never exposed via the public API."""
        session = self.db.get(models.GameSession, session_id)
        if session is None:
            raise KeyError(session_id)
        rnd = self._current_round(session)
        return list(rnd.sequence) if rnd else []

    def submit_answer(self, session_id: str, round_id: str, transcript: str) -> AnswerResult:
        session = self.db.get(models.GameSession, session_id)
        if session is None:
            raise KeyError(session_id)
        rnd = self.db.get(models.Round, round_id)
        if rnd is None or rnd.session_id != session_id:
            raise KeyError(round_id)

        # --- idempotency guard: a round already answered returns its stored result ---
        if rnd.status != "PENDING" and rnd.response is not None:
            return self._result_from_stored(session, rnd)

        ev = engine.evaluate(list(rnd.sequence), transcript)

        response = models.Response(
            round_id=rnd.id, transcript=transcript, normalized=ev.heard,
            is_correct=ev.is_correct, points_awarded=ev.points,
        )
        self.db.add(response)
        rnd.status = "CORRECT" if ev.is_correct else "WRONG"

        if ev.is_correct:
            session.score += ev.points
            session.current_round += 1
            self.db.flush()
            self._new_round(session)            # prepare the next round
        else:
            session.status = "ENDED"
            from datetime import datetime, timezone
            session.ended_at = datetime.now(timezone.utc)

        self.db.commit()

        # refresh caches
        if session.status == "ACTIVE":
            store.set_active_session(session.id, self._state(session).model_dump())
        else:
            store.drop_active_session(session.id)
            store.invalidate_leaderboard()

        return AnswerResult(
            session_id=session.id, round_number=rnd.round_number,
            is_correct=ev.is_correct, points_awarded=ev.points,
            total_score=session.score, status=session.status,
            expected=ev.expected, heard=ev.heard,
        )

    def _result_from_stored(self, session: models.GameSession, rnd: models.Round) -> AnswerResult:
        resp = rnd.response
        return AnswerResult(
            session_id=session.id, round_number=rnd.round_number,
            is_correct=resp.is_correct, points_awarded=resp.points_awarded,
            total_score=session.score, status=session.status,
            expected=list(rnd.sequence), heard=list(resp.normalized),
        )

    def end_session(self, session_id: str) -> SessionState:
        session = self.db.get(models.GameSession, session_id)
        if session is None:
            raise KeyError(session_id)
        if session.status == "ACTIVE":
            session.status = "ENDED"
            from datetime import datetime, timezone
            session.ended_at = datetime.now(timezone.utc)
            self.db.commit()
        store.drop_active_session(session_id)
        store.invalidate_leaderboard()
        return self._state(session)

    def leaderboard(self, limit: int = 10) -> List[LeaderboardEntry]:
        cached = store.get_leaderboard()
        if cached is not None:
            return [LeaderboardEntry(**row) for row in cached[:limit]]
        rows = (
            self.db.query(models.GameSession)
            .order_by(models.GameSession.score.desc(), models.GameSession.created_at.asc())
            .limit(limit)
            .all()
        )
        entries = [LeaderboardEntry(player_name=r.player_name, score=r.score) for r in rows]
        store.set_leaderboard([e.model_dump() for e in entries])
        return entries
