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
