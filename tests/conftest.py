import fakeredis
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base


@pytest.fixture(autouse=True, scope="session")
def _fake_redis():
    """Swap the real Redis client for an in-memory fake for the whole test
    session, so pytest never needs a real Redis instance running (mirrors how
    the `db` fixture below uses SQLite instead of requiring Postgres)."""
    from app.cache import store

    store._redis = fakeredis.FakeRedis(decode_responses=True)
    yield


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, future=True)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
