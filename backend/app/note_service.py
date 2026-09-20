"""Multi-note write/read logic. Routers stay thin; every mutation goes through here.

Locking contract: each write first calls next_seq(), whose UPDATE on the user's
`user_sync` row takes a per-user lock held until commit. Everything after that
(load note, check rev, apply) is therefore serialised per user, and seq numbers
are committed in order. Anything that raises before commit rolls the seq back.
"""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .models import Note, NoteRevision, User, UserSync
from .schemas import NoteV2, NotePut, NoteTransition, RevisionOut

_SHRINK_MIN_CHARS = 200  # only guard notes that had real content
_SHRINK_RATIO = 0.5  # new content under half the old size counts as a big shrink


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat them as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def derive_title(content: str) -> str:
    """Same rule as the desktop client: first non-empty line, markdown prefix stripped."""
    for line in (content or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped.lstrip("#*->+ ").strip()[:40]
    return ""


def normalize_note_id(raw: str) -> str:
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Note id must be a UUID")


def to_out(note: Note) -> NoteV2:
    return NoteV2(
        id=note.id,
        title=note.title,
        content=note.content,
        position=note.position,
        rev=note.rev,
        seq=note.seq,
        deleted=note.deleted_at is not None,
        purged=note.purged_at is not None,
        deleted_at=note.deleted_at,
        created_at=note.created_at,
        updated_at=note.updated_at,
        updated_by_device=note.updated_by_device,
    )


def _conflict(note: Note) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={"error": "conflict", "note": jsonable_encoder(to_out(note))},
    )


def next_seq(db: Session, user_id: int) -> int:
    """Allocate the user's next change number and take their write lock (see module doc)."""

    def bump():
        return db.execute(
            update(UserSync)
            .where(UserSync.user_id == user_id)
            .values(seq=UserSync.seq + 1)
            .returning(UserSync.seq)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()

    seq = bump()
    if seq is None:
        try:
            with db.begin_nested():
                db.add(UserSync(user_id=user_id, seq=1, tombstone_floor=0))
                db.flush()
            return 1
        except IntegrityError:  # another request created the row first
            seq = bump()
    return seq


def _load_locked(db: Session, user_id: int, note_id: str) -> Note | None:
    # populate_existing: we may have a stale copy from before we won the lock.
    return db.get(Note, (user_id, note_id), populate_existing=True)


def _snapshot_before_overwrite(db: Session, note: Note, new_content: str, now: datetime) -> None:
    """Keep history: save the version about to be replaced (see settings.revision_*)."""
    if not note.content or note.content == new_content:
        return
    last_created = db.scalar(
        select(func.max(NoteRevision.created_at)).where(
            NoteRevision.user_id == note.user_id, NoteRevision.note_id == note.id
        )
    )
    window_passed = (
        last_created is None
        or (now - aware(last_created)).total_seconds() >= settings.revision_coalesce_seconds
    )
    big_shrink = (
        len(note.content) >= _SHRINK_MIN_CHARS
        and len(new_content) < len(note.content) * _SHRINK_RATIO
    )
    if not (window_passed or big_shrink):
        return
    db.add(
        NoteRevision(
            user_id=note.user_id,
            note_id=note.id,
            rev=note.rev,
            content=note.content,
            device_id=note.updated_by_device,
            created_at=aware(note.updated_at),
        )
    )
    db.flush()
    excess = db.scalars(
        select(NoteRevision.id)
        .where(NoteRevision.user_id == note.user_id, NoteRevision.note_id == note.id)
        .order_by(NoteRevision.created_at.desc(), NoteRevision.id.desc())
        .offset(settings.revision_keep)
    ).all()
    if excess:
        db.execute(delete(NoteRevision).where(NoteRevision.id.in_(excess)))


def _finish_noop(db: Session, user_id: int, note_id: str) -> Note:
    db.rollback()  # discard the seq we took; nothing changed
    return db.get(Note, (user_id, note_id), populate_existing=True)


# ---------------------------------------------------------------- writes

def put_note(db: Session, user: User, note_id: str, payload: NotePut) -> Note:
    note_id = normalize_note_id(note_id)
    if len(payload.content.encode("utf-8")) > settings.max_note_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Note too large")

    now = utcnow()
    seq = next_seq(db, user.id)
    note = _load_locked(db, user.id, note_id)

    if note is not None and payload.mutation_id and note.last_mutation_id == payload.mutation_id:
        return _finish_noop(db, user.id, note_id)  # retry of a request we already applied

    if note is None:
        if payload.base_rev != 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
        live_count = db.scalar(
            select(func.count()).select_from(Note).where(
                Note.user_id == user.id, Note.purged_at.is_(None)
            )
        )
        if live_count >= settings.max_notes_per_user:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Note limit reached")
        note = Note(
            user_id=user.id, id=note_id, content=payload.content,
            title=derive_title(payload.content), position=payload.position or "",
            rev=1, seq=seq, created_at=now, updated_at=now,
            updated_by_device=payload.device_id, last_mutation_id=payload.mutation_id,
        )
        db.add(note)
    else:
        if note.purged_at is not None or payload.base_rev != note.rev:
            raise _conflict(note)
        if note.deleted_at is not None and not payload.restore:
            raise _conflict(note)  # edits never silently resurrect a trashed note
        if payload.content == note.content and note.deleted_at is None:
            return _finish_noop(db, user.id, note_id)
        _snapshot_before_overwrite(db, note, payload.content, now)
        note.content = payload.content
        note.title = derive_title(payload.content)
        note.rev += 1
        note.seq = seq
        note.updated_at = now
        note.updated_by_device = payload.device_id
        note.last_mutation_id = payload.mutation_id
        note.deleted_at = None
        if payload.position is not None:
            note.position = payload.position

    db.commit()
    return db.get(Note, (user.id, note_id), populate_existing=True)


def set_trashed(db: Session, user: User, note_id: str, trashed: bool, payload: NoteTransition) -> Note:
    note_id = normalize_note_id(note_id)
    now = utcnow()
    seq = next_seq(db, user.id)
    note = _load_locked(db, user.id, note_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")

    if payload.mutation_id and note.last_mutation_id == payload.mutation_id:
        return _finish_noop(db, user.id, note_id)
    if note.purged_at is not None or payload.base_rev != note.rev:
        raise _conflict(note)
    if (note.deleted_at is not None) == trashed:
        return _finish_noop(db, user.id, note_id)  # already in the requested state

    note.deleted_at = now if trashed else None
    note.rev += 1
    note.seq = seq
    note.updated_by_device = payload.device_id
    note.last_mutation_id = payload.mutation_id
    db.commit()
    return db.get(Note, (user.id, note_id), populate_existing=True)


def set_position(db: Session, user: User, note_id: str, position: str) -> Note:
    """Reordering is last-write-wins and never touches content or rev."""
    note_id = normalize_note_id(note_id)
    seq = next_seq(db, user.id)
    note = _load_locked(db, user.id, note_id)
    if note is None or note.purged_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    if note.position == position:
        return _finish_noop(db, user.id, note_id)
    note.position = position
    note.seq = seq
    db.commit()
    return db.get(Note, (user.id, note_id), populate_existing=True)


# ---------------------------------------------------------------- reads

def get_note(db: Session, user: User, note_id: str) -> Note:
    note = db.get(Note, (user.id, normalize_note_id(note_id)))
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    return note


def list_changes(db: Session, user: User, cursor: int, limit: int):
    """Delta feed: notes with seq > cursor in seq order. cursor 0 means "from scratch"."""
    sync = db.get(UserSync, user.id)
    current = sync.seq if sync else 0
    floor = sync.tombstone_floor if sync else 0
    # cursor ahead of the server => the DB was restored from a backup; cursor behind
    # the floor => a deletion it never saw has been garbage-collected. Both need a resync.
    if cursor > current or (cursor != 0 and cursor < floor):
        raise HTTPException(status.HTTP_410_GONE, "cursor_expired")

    rows = db.scalars(
        select(Note).where(Note.user_id == user.id, Note.seq > cursor).order_by(Note.seq).limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return [to_out(n) for n in rows], (rows[-1].seq if rows else cursor), has_more


def list_revisions(db: Session, user: User, note_id: str) -> list[RevisionOut]:
    note = get_note(db, user, note_id)
    revs = db.scalars(
        select(NoteRevision)
        .where(NoteRevision.user_id == user.id, NoteRevision.note_id == note.id)
        .order_by(NoteRevision.created_at.desc(), NoteRevision.id.desc())
    ).all()
    return [
        RevisionOut(id=r.id, rev=r.rev, created_at=r.created_at, device_id=r.device_id, size=len(r.content))
        for r in revs
    ]


def get_revision(db: Session, user: User, note_id: str, revision_id: int) -> NoteRevision:
    note = get_note(db, user, note_id)
    rev = db.get(NoteRevision, revision_id)
    if rev is None or rev.user_id != user.id or rev.note_id != note.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Revision not found")
    return rev
