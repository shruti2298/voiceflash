import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, JSON, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GameSession(Base):
    __tablename__ = "game_sessions"

    id = Column(String, primary_key=True, default=_uuid)
    player_name = Column(String, nullable=False)
    status = Column(String, nullable=False, default="ACTIVE")   # ACTIVE | ENDED
    score = Column(Integer, nullable=False, default=0)
    current_round = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=_now)
    ended_at = Column(DateTime(timezone=True), nullable=True)

    rounds = relationship("Round", back_populates="session", cascade="all, delete-orphan")


class Round(Base):
    __tablename__ = "rounds"

    id = Column(String, primary_key=True, default=_uuid)
    session_id = Column(String, ForeignKey("game_sessions.id"), nullable=False)
    round_number = Column(Integer, nullable=False)
    sequence = Column(JSON, nullable=False)                     # e.g. ["apple","tiger","river"]
    status = Column(String, nullable=False, default="PENDING")  # PENDING | CORRECT | WRONG
    created_at = Column(DateTime(timezone=True), default=_now)

    session = relationship("GameSession", back_populates="rounds")
    response = relationship("Response", back_populates="round", uselist=False,
                            cascade="all, delete-orphan")


class Response(Base):
    __tablename__ = "responses"
    __table_args__ = (UniqueConstraint("round_id", name="uq_response_per_round"),)

    id = Column(String, primary_key=True, default=_uuid)
    round_id = Column(String, ForeignKey("rounds.id"), nullable=False)
    transcript = Column(String, nullable=False)
    normalized = Column(JSON, nullable=False)                   # tokenized answer
    is_correct = Column(Boolean, nullable=False)
    points_awarded = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=_now)

    round = relationship("Round", back_populates="response")
