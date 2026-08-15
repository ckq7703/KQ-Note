from fastapi import FastAPI

from .database import Base, engine
from .routers import auth, notes

# v1: create tables directly on startup instead of Alembic migrations.
Base.metadata.create_all(bind=engine)

app = FastAPI(title="KQ Note Sync API", version="0.1.0")
app.include_router(auth.router)
app.include_router(notes.router)


@app.get("/health")
def health():
    return {"status": "ok"}
