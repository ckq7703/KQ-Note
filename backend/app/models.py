from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import relationship

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

    note = relationship("Note", back_populates="user", uselist=False, cascade="all, delete-orphan")


class Note(Base):
    """Each user has exactly one note blob, matching the desktop app's single-note model."""

    __tablename__ = "notes"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    content = Column(Text, nullable=False, default="")
    version = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    updated_by_device = Column(String, nullable=True)

    user = relationship("User", back_populates="note")


class Image(Base):
    """Pasted-image blobs, keyed by the same file_id the desktop app already
    uses locally (app/store.py images/<file_id>.png). Composite PK (not just
    file_id) so two different accounts can never collide on a uuid."""

    __tablename__ = "images"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    id = Column(String, primary_key=True)
    data = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
