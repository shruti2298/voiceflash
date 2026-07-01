import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.cache import store


@pytest.fixture()
def client():
    # Import app.main FIRST: it pulls in app.game.service -> app.db.models,
    # which is what registers the ORM tables on Base.metadata. Doing this
    # before create_all() matters when test_api.py runs in isolation (e.g.
    # `pytest tests/test_api.py`), since no other test module has imported
    # the models yet to register them as a side effect.
    from app.main import app

    # StaticPool is required here (unlike tests/conftest.py's single-threaded `db`
    # fixture): TestClient runs sync endpoints via anyio's threadpool, and the
    # default SQLite SingletonThreadPool hands that worker thread a brand-new,
    # empty :memory: database. StaticPool shares one connection across threads.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, future=True)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    # Importing app.main is Postgres-free: table creation lives in the lifespan
    # handler, and TestClient(app) below is NOT used as a context manager, so
    # the lifespan never fires. Tables were already created on the SQLite engine
    # above; the get_db override routes all queries there.
    app.dependency_overrides[get_db] = override_get_db
    store.clear_all()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_start_and_get_state(client):
    r = client.post("/api/sessions", json={"player_name": "Ann"})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    s = client.get(f"/api/sessions/{sid}")
    assert s.status_code == 200
    assert s.json()["current_round"] == 1
    # the words are never leaked, only the length
    assert "sequence" not in s.json()
    assert s.json()["sequence_length"] == 3


def test_end_session_and_leaderboard(client):
    sid = client.post("/api/sessions", json={"player_name": "Ann"}).json()["session_id"]
    client.post(f"/api/sessions/{sid}/end")
    lb = client.get("/api/leaderboard").json()
    assert any(e["player_name"] == "Ann" for e in lb)
