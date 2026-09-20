"""Startup schema migrations. The project has no Alembic yet, so this handles the one
structural change create_all() can't: the single-note `notes` table becoming multi-note.
"""

import logging
import uuid

from sqlalchemy import inspect, insert, select, text

from .database import Base
from .models import LegacyNote, Note, UserSync
from .note_service import derive_title

log = logging.getLogger("kqnote.migrations")

_FIRST_POSITION = "a0"


def migrate_single_note_to_multi(engine) -> int:
    """Rename the v1 `notes` table to `notes_legacy` and copy each non-empty blob into a
    v2 note. Returns how many notes were created (0 when nothing to do).

    Everything runs in one transaction (Postgres DDL is transactional), so a failure
    leaves the v1 table exactly as it was. `notes_legacy` is never dropped: the old
    /notes/me endpoint keeps serving 1.4.x clients from it, and it is the rollback copy.
    """
    insp = inspect(engine)
    if not insp.has_table("notes"):
        return 0
    if "id" in {c["name"] for c in insp.get_columns("notes")}:
        return 0  # already the v2 shape
    if insp.has_table("notes_legacy"):
        raise RuntimeError(
            "Found a v1-shaped `notes` table and an existing `notes_legacy`; "
            "refusing to guess. Resolve manually."
        )

    created = 0
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE notes RENAME TO notes_legacy"))
        if conn.dialect.name == "postgresql":
            # Index names are schema-global; free `notes_pkey` for the new table.
            conn.execute(text("ALTER INDEX IF EXISTS notes_pkey RENAME TO notes_legacy_pkey"))
        Base.metadata.create_all(bind=conn)

        legacy = conn.execute(select(LegacyNote.__table__)).mappings().all()
        for row in legacy:
            content = row["content"] or ""
            if not content.strip():
                continue  # nothing worth carrying over (e.g. a never-used account)
            user_id = row["user_id"]
            conn.execute(insert(UserSync.__table__).values(user_id=user_id, seq=1, tombstone_floor=0))
            conn.execute(
                insert(Note.__table__).values(
                    user_id=user_id,
                    id=str(uuid.uuid4()),
                    title=derive_title(content),
                    content=content,
                    position=_FIRST_POSITION,
                    rev=1,
                    seq=1,
                    created_at=row["updated_at"],
                    updated_at=row["updated_at"],
                    updated_by_device=row["updated_by_device"],
                )
            )
            created += 1
    log.info("migrated %d single-note blobs into multi-note rows", created)
    return created


def run_startup_migrations(engine):
    migrate_single_note_to_multi(engine)
    Base.metadata.create_all(bind=engine)
