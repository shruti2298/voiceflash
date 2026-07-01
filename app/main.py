from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db.database import Base, engine
from app.api.routes import router as api_router
from app.voice.webrtc import router as webrtc_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Create tables on startup (dev convenience; use migrations for prod).
    # This lives in the lifespan handler — NOT at import time — so importing
    # app.main never touches Postgres. TestClient only fires the lifespan when
    # used as a context manager, so unit tests (which don't) stay Postgres-free.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Memory Card Voice Bot", lifespan=lifespan)
app.include_router(api_router, prefix="/api")
app.include_router(webrtc_router, prefix="/rtc")
app.mount("/", StaticFiles(directory="static", html=True), name="static")
