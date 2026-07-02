from typing import List, Optional
from pydantic import BaseModel


class StartSessionRequest(BaseModel):
    player_name: str


class SubmitAnswerRequest(BaseModel):
    transcript: str


class SessionState(BaseModel):
    session_id: str
    player_name: str
    status: str
    score: int
    current_round: int
    round_id: Optional[str] = None
    sequence_length: Optional[int] = None   # length only; never leak the *current* words to the UI
    # The most recently answered round's words — only ever the round already
    # answered, never the pending one, so this doesn't leak an unanswered
    # sequence. None until the first answer is submitted.
    last_expected: Optional[List[str]] = None
    last_heard: Optional[List[str]] = None
    last_correct: Optional[bool] = None


class AnswerResult(BaseModel):
    session_id: str
    round_number: int
    is_correct: bool
    points_awarded: int
    total_score: int
    status: str                              # ACTIVE (advanced) | ENDED (game over)
    expected: List[str]                      # revealed after answering, for feedback
    heard: List[str]


class LeaderboardEntry(BaseModel):
    player_name: str
    score: int
