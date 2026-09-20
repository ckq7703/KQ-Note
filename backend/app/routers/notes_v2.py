from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import note_service as svc
from ..database import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import (
    ChangesOut,
    NotePosition,
    NotePut,
    NoteTransition,
    NoteV2,
    RevisionDetail,
    RevisionOut,
)

router = APIRouter(prefix="/v2/notes", tags=["notes-v2"])


@router.get("/changes", response_model=ChangesOut)
def changes(
    cursor: int = Query(0, ge=0, description="seq of the last change already applied; 0 = from scratch"),
    limit: int = Query(200, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notes, next_cursor, has_more = svc.list_changes(db, user, cursor, limit)
    return ChangesOut(changes=notes, cursor=next_cursor, has_more=has_more)


@router.get("/{note_id}", response_model=NoteV2)
def get_note(note_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return svc.to_out(svc.get_note(db, user, note_id))


@router.put("/{note_id}", response_model=NoteV2)
def put_note(
    note_id: str,
    payload: NotePut,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create (base_rev=0) or update. A stale base_rev returns 409 with the server's copy
    and never overwrites anything."""
    return svc.to_out(svc.put_note(db, user, note_id, payload))


@router.post("/{note_id}/trash", response_model=NoteV2)
def trash_note(
    note_id: str,
    payload: NoteTransition,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return svc.to_out(svc.set_trashed(db, user, note_id, True, payload))


@router.post("/{note_id}/restore", response_model=NoteV2)
def restore_note(
    note_id: str,
    payload: NoteTransition,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return svc.to_out(svc.set_trashed(db, user, note_id, False, payload))


@router.patch("/{note_id}", response_model=NoteV2)
def move_note(
    note_id: str,
    payload: NotePosition,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return svc.to_out(svc.set_position(db, user, note_id, payload.position))


@router.get("/{note_id}/revisions", response_model=list[RevisionOut])
def revisions(note_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return svc.list_revisions(db, user, note_id)


@router.get("/{note_id}/revisions/{revision_id}", response_model=RevisionDetail)
def revision(
    note_id: str,
    revision_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    r = svc.get_revision(db, user, note_id, revision_id)
    return RevisionDetail(
        id=r.id, rev=r.rev, created_at=r.created_at, device_id=r.device_id,
        size=len(r.content), content=r.content,
    )
