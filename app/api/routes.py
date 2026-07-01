from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.game.service import GameService
from app.schemas import (
    AnswerResult, LeaderboardEntry, SessionState, StartSessionRequest, SubmitAnswerRequest,
)

router = APIRouter()


def _svc(db: Session = Depends(get_db)) -> GameService:
    return GameService(db)


@router.post("/sessions", response_model=SessionState)
def start_session(body: StartSessionRequest, svc: GameService = Depends(_svc)):
    return svc.start_session(body.player_name)


@router.get("/sessions/{session_id}", response_model=SessionState)
def get_state(session_id: str, svc: GameService = Depends(_svc)):
    try:
        return svc.get_state(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


@router.post("/sessions/{session_id}/answer", response_model=AnswerResult)
def submit_answer(session_id: str, body: SubmitAnswerRequest, svc: GameService = Depends(_svc)):
    """Text answer endpoint — used by the frontend for manual testing and by tests.
    The voice bot calls GameService directly."""
    state = svc.get_state(session_id)
    if state.round_id is None:
        raise HTTPException(status_code=409, detail="no active round")
    return svc.submit_answer(session_id, state.round_id, body.transcript)


@router.post("/sessions/{session_id}/end", response_model=SessionState)
def end_session(session_id: str, svc: GameService = Depends(_svc)):
    try:
        return svc.end_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


@router.get("/leaderboard", response_model=list[LeaderboardEntry])
def leaderboard(limit: int = 10, svc: GameService = Depends(_svc)):
    return svc.leaderboard(limit)
