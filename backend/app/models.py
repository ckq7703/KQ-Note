from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)

from .database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=True)  # null for Google-only accounts
    google_sub = Column(String, unique=True, index=True, nullable=True)
    avatar_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class LegacyNote(Base):
    """v1 single-slot note (one blob per user), served only to 1.4.x clients via /notes/me.

    The pre-multi-note `notes` table is renamed to this by app.migrations; v2 clients
    never touch it.
    """

    __tablename__ = "notes_legacy"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    content = Column(Text, nullable=False, default="")
    version = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    updated_by_device = Column(String, nullable=True)


class UserSync(Base):
    """Per-user change counter (the delta cursor) and the tombstone horizon.

    Every accepted write does `UPDATE ... SET seq = seq + 1` on this row first, which
    both hands out the next seq and serialises that user's writers until commit, so
    seq order always equals commit order and a poller can never skip a change.
    """

    __tablename__ = "user_sync"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    seq = Column(BigInteger, nullable=False, default=0)
    # Highest seq of a hard-deleted tombstone. A cursor below this may have missed
    # a deletion, so the client must do a full resync (HTTP 410).
    tombstone_floor = Column(BigInteger, nullable=False, default=0)


class Note(Base):
    """One note. `id` is a client-generated UUID, unique per user (composite PK, like Image).

    rev  - content version, bumped by content/trash/restore changes; the optimistic-
           concurrency token clients send back as `base_rev`.
    seq  - position in the user's change feed; also moves on metadata-only changes
           (reordering), which do not touch rev.
    State: live (deleted_at NULL) -> trashed (deleted_at set, content kept) ->
           purged (purged_at set, content emptied; row kept as a tombstone).
    """

    __tablename__ = "notes"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    id = Column(String(36), primary_key=True)
    title = Column(String, nullable=False, default="")
    content = Column(Text, nullable=False, default="")
    position = Column(String(64), nullable=False, default="")
    rev = Column(Integer, nullable=False, default=1)
    seq = Column(BigInteger, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    purged_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_by_device = Column(String, nullable=True)
    # Lets a client retry a request whose response was lost without double-applying it.
    last_mutation_id = Column(String, nullable=True)

    __table_args__ = (Index("ix_notes_user_seq", "user_id", "seq"),)


class NoteRevision(Base):
    """A superseded version of a note (history), taken just before it was overwritten."""

    __tablename__ = "note_revisions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    note_id = Column(String(36), nullable=False)
    rev = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    device_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)  # when this version was written

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "note_id"], ["notes.user_id", "notes.id"], ondelete="CASCADE"
        ),
        Index("ix_note_revisions_note", "user_id", "note_id", "created_at"),
    )


class Image(Base):
    """Pasted-image blobs, keyed by the same file_id the desktop app already
    uses locally (app/store.py images/<file_id>.png). Composite PK (not just
    file_id) so two different accounts can never collide on a uuid."""

    __tablename__ = "images"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    id = Column(String, primary_key=True)
    data = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
