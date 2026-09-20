import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database import engine
from .maintenance import maintenance_loop
from .migrations import run_startup_migrations
from .routers import auth, images, notes, notes_v2

# v1: no Alembic; this renames the single-note table to multi-note when needed and
# creates any missing tables.
run_startup_migrations(engine)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(maintenance_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="KQ Note Sync API", version="0.2.0", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(notes.router)  # legacy v1 single-slot, for 1.4.x clients
app.include_router(notes_v2.router)
app.include_router(images.router)


@app.get("/health")
def health():
    return {"status": "ok"}
