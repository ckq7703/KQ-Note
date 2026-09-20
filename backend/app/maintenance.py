"""Retention jobs: purge old trash, garbage-collect tombstones, thin out old history.

Each note is handled in its own transaction so a user's write lock is held only
briefly and one bad row can't block the rest.
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import and_, delete, func, select, update

from .config import settings
from .database import SessionLocal
from .models import Note, NoteRevision, UserSync
from .note_service import apply_purge, aware, next_seq, utcnow

log = logging.getLogger("kqnote.maintenance")


def purge_expired_trash(db, now=None) -> int:
    """Trashed longer than trash_retention_days -> purged (content dropped, row kept as tombstone)."""
    now = now or utcnow()
    cutoff = now - timedelta(days=settings.trash_retention_days)
    keys = db.execute(
        select(Note.user_id, Note.id).where(
            Note.deleted_at.is_not(None), Note.purged_at.is_(None), Note.deleted_at < cutoff
        )
    ).all()
    purged = 0
    for user_id, note_id in keys:
        seq = next_seq(db, user_id)
        note = db.get(Note, (user_id, note_id), populate_existing=True)
        if (
            note is None
            or note.purged_at is not None
            or note.deleted_at is None
            or aware(note.deleted_at) >= cutoff
        ):
            db.rollback()  # changed under us (restored/edited): leave it alone
            continue
        apply_purge(db, note, seq, now)
        db.commit()
        purged += 1
    return purged


def drop_old_tombstones(db, now=None) -> int:
    """Delete purged rows past tombstone_retention_days and raise the user's floor so
    any client that could have missed the deletion is told to resync (HTTP 410)."""
    now = now or utcnow()
    cutoff = now - timedelta(days=settings.tombstone_retention_days)
    rows = db.execute(
        select(Note.user_id, Note.id, Note.seq).where(Note.purged_at.is_not(None), Note.purged_at < cutoff)
    ).all()
    for user_id, note_id, seq in rows:
        next_seq(db, user_id)  # take the user's write lock
        db.execute(
            update(UserSync)
            .where(UserSync.user_id == user_id, UserSync.tombstone_floor < seq)
            .values(tombstone_floor=seq)
        )
        db.execute(delete(Note).where(Note.user_id == user_id, Note.id == note_id))
        db.commit()
    return len(rows)


def prune_old_revisions(db, now=None) -> int:
    """Drop history older than revision_retention_days, always keeping the newest few per note."""
    now = now or utcnow()
    cutoff = now - timedelta(days=settings.revision_retention_days)
    ranked = select(
        NoteRevision.id,
        NoteRevision.created_at,
        func.row_number().over(
            partition_by=(NoteRevision.user_id, NoteRevision.note_id),
            order_by=(NoteRevision.created_at.desc(), NoteRevision.id.desc()),
        ).label("rn"),
    ).subquery()
    old_ids = db.scalars(
        select(ranked.c.id).where(and_(ranked.c.rn > settings.revision_keep_min, ranked.c.created_at < cutoff))
    ).all()
    if old_ids:
        db.execute(delete(NoteRevision).where(NoteRevision.id.in_(old_ids)))
        db.commit()
    return len(old_ids)


def run_maintenance_once():
    db = SessionLocal()
    try:
        result = {
            "purged": purge_expired_trash(db),
            "tombstones_dropped": drop_old_tombstones(db),
            "revisions_pruned": prune_old_revisions(db),
        }
        log.info("maintenance done: %s", result)
        return result
    finally:
        db.close()


async def maintenance_loop(interval_seconds: int = 6 * 3600, initial_delay: int = 60):
    await asyncio.sleep(initial_delay)
    while True:
        try:
            await asyncio.to_thread(run_maintenance_once)
        except Exception:  # never let the loop die; try again next interval
            log.exception("maintenance run failed")
        await asyncio.sleep(interval_seconds)
