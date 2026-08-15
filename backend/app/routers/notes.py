from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import Note, User
from ..schemas import NoteOut, NoteUpdate

router = APIRouter(prefix="/notes", tags=["notes"])


@router.get("/me", response_model=NoteOut)
def get_my_note(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    note = db.get(Note, user.id)
    if note is None:
        # Should not happen (a note row is created at registration), but degrade gracefully.
        note = Note(user_id=user.id, content="", version=0)
        db.add(note)
        db.commit()
        db.refresh(note)
    return note


@router.put("/me", response_model=NoteOut)
def update_my_note(
    payload: NoteUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    note = db.get(Note, user.id)
    if note is None:
        note = Note(user_id=user.id, content="", version=0)
        db.add(note)
        db.flush()

    if payload.base_version != note.version:
        # Optimistic-concurrency conflict: caller's copy is stale. Hand back the
        # current server state (as the same shape as NoteOut) so the client can merge.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "content": note.content,
                "version": note.version,
                "updated_at": note.updated_at.isoformat(),
            },
        )

    note.content = payload.content
    note.version += 1
    note.updated_by_device = payload.device_id
    db.commit()
    db.refresh(note)
    return note
