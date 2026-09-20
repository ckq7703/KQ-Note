from datetime import timedelta

import pytest

from app import maintenance, note_service
from app.config import settings
from app.database import SessionLocal
from app.models import Note, NoteRevision, UserSync

from conftest import IS_POSTGRES, new_id


# ------------------------------------------------------------ create / update / conflict

def test_create_then_read_and_appears_in_changes(a):
    nid, note = a.create("# Title\nbody")
    assert note["rev"] == 1 and note["title"] == "Title" and not note["deleted"]
    assert a.get(nid).json()["content"] == "# Title\nbody"

    feed = a.changes(0).json()
    assert [n["id"] for n in feed["changes"]] == [nid]
    assert feed["cursor"] == note["seq"] and feed["has_more"] is False


def test_update_with_current_rev_bumps_rev_and_seq(a):
    nid, n1 = a.create("v1")
    r = a.put(nid, "v2", base_rev=1)
    assert r.status_code == 200
    n2 = r.json()
    assert n2["rev"] == 2 and n2["seq"] > n1["seq"] and n2["content"] == "v2"


def test_stale_base_rev_returns_409_and_never_overwrites(a):
    nid, _ = a.create("v1")
    a.put(nid, "v2 from device A", base_rev=1)

    r = a.put(nid, "v2 from device B", base_rev=1)  # B never saw rev 2

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "conflict"
    assert detail["note"]["content"] == "v2 from device A" and detail["note"]["rev"] == 2
    assert a.get(nid).json()["content"] == "v2 from device A"


def test_update_of_unknown_note_is_404_but_create_with_rev0_works(a):
    assert a.put(new_id(), "x", base_rev=3).status_code == 404
    assert a.put(new_id(), "x", base_rev=0).status_code == 200


def test_creating_an_existing_id_with_rev0_conflicts(a):
    nid, _ = a.create("mine")
    assert a.put(nid, "other", base_rev=0).status_code == 409


def test_same_mutation_id_is_applied_once(a):
    nid, _ = a.create("v1")
    first = a.put(nid, "v2", base_rev=1, mutation_id="m-1")
    replay = a.put(nid, "v2", base_rev=1, mutation_id="m-1")  # response was lost, client retries

    assert first.status_code == replay.status_code == 200
    assert replay.json()["rev"] == first.json()["rev"] == 2
    assert a.get(nid).json()["rev"] == 2


def test_unchanged_content_is_a_noop(a):
    nid, n1 = a.create("same")
    r = a.put(nid, "same", base_rev=1)
    assert r.status_code == 200 and r.json()["rev"] == 1 and r.json()["seq"] == n1["seq"]


def test_notes_are_isolated_between_users(a, b):
    nid, _ = a.create("alice secret")
    assert b.get(nid).status_code == 404
    assert b.changes(0).json()["changes"] == []
    # bob can use the same id for his own note without touching alice's
    assert b.put(nid, "bob note", base_rev=0).status_code == 200
    assert a.get(nid).json()["content"] == "alice secret"


def test_validation_limits(a, monkeypatch):
    assert a.put("not-a-uuid", "x", 0).status_code == 422
    monkeypatch.setattr(settings, "max_note_bytes", 10)
    assert a.put(new_id(), "x" * 11, 0).status_code == 413
    monkeypatch.setattr(settings, "max_notes_per_user", 1)
    a.create("first")
    assert a.put(new_id(), "second", 0).status_code == 403


def test_requires_auth(client):
    assert client.get("/v2/notes/changes").status_code in (401, 403)


# ------------------------------------------------------------ trash / restore

def test_trash_and_restore_roundtrip(a):
    nid, _ = a.create("keep me")
    t = a.trash(nid, base_rev=1)
    assert t.status_code == 200 and t.json()["deleted"] and t.json()["rev"] == 2
    assert t.json()["content"] == "keep me"  # trash keeps content

    assert a.changes(0).json()["changes"][0]["deleted"] is True

    r = a.restore(nid, base_rev=2)
    assert r.status_code == 200 and not r.json()["deleted"] and r.json()["rev"] == 3


def test_edit_of_trashed_note_conflicts_unless_restore_flag(a):
    nid, _ = a.create("v1")
    a.trash(nid, base_rev=1)

    assert a.put(nid, "edited", base_rev=2).status_code == 409
    r = a.put(nid, "edited", base_rev=2, restore=True)
    assert r.status_code == 200 and not r.json()["deleted"] and r.json()["content"] == "edited"


def test_delete_from_stale_device_is_rejected_and_keeps_the_newer_edit(a):
    nid, _ = a.create("v1")
    a.put(nid, "v2 typed on another device", base_rev=1)

    r = a.trash(nid, base_rev=1)  # stale device tries to delete

    assert r.status_code == 409
    assert a.get(nid).json()["deleted"] is False


def test_trash_is_idempotent(a):
    nid, _ = a.create("x")
    a.trash(nid, base_rev=1, mutation_id="t1")
    again = a.trash(nid, base_rev=1, mutation_id="t1")
    assert again.status_code == 200 and again.json()["rev"] == 2


# ------------------------------------------------------------ purge ("delete forever")

def purge(a, nid, base_rev, **kw):
    return a.c.post(f"/v2/notes/{nid}/purge", headers=a.h, json={"base_rev": base_rev, **kw})


def test_purge_a_trashed_note_drops_content_and_history_at_once(a):
    nid, _ = a.create("secret text " * 30)
    a.put(nid, "tiny", base_rev=1)  # leaves a history snapshot
    a.trash(nid, base_rev=2)
    before = a.get(nid).json()

    r = purge(a, nid, before["rev"])

    assert r.status_code == 200
    n = r.json()
    assert n["purged"] and n["content"] == "" and n["title"] == ""
    assert n["rev"] == before["rev"] + 1 and n["seq"] > before["seq"]
    assert a.revisions(nid).json() == []
    assert a.changes(before["seq"]).json()["changes"][0]["purged"] is True
    assert a.put(nid, "zombie", base_rev=n["rev"]).status_code == 409


def test_a_live_note_cannot_be_purged(a):
    nid, _ = a.create("keep me")
    assert purge(a, nid, 1).status_code == 409
    assert a.get(nid).json()["content"] == "keep me"


def test_purge_from_a_stale_revision_is_refused(a):
    nid, _ = a.create("v1")
    a.trash(nid, base_rev=1)
    a.restore(nid, base_rev=2)  # someone brought it back
    a.trash(nid, base_rev=3)
    assert purge(a, nid, 2).status_code == 409  # this device only saw rev 2
    assert a.get(nid).json()["content"] == "v1"


def test_purge_is_idempotent_and_404_for_unknown_notes(a):
    nid, _ = a.create("x")
    a.trash(nid, base_rev=1)
    assert purge(a, nid, 2, mutation_id="p1").status_code == 200
    again = purge(a, nid, 2, mutation_id="p1")
    assert again.status_code == 200 and again.json()["purged"] and again.json()["rev"] == 3
    assert purge(a, new_id(), 1).status_code == 404


def test_users_cannot_purge_each_others_notes(a, b):
    nid, _ = a.create("alice's")
    a.trash(nid, base_rev=1)
    assert purge(b, nid, 2).status_code == 404
    assert a.get(nid).json()["purged"] is False


# ------------------------------------------------------------ delta feed

def test_changes_paging_has_no_gaps_or_duplicates(a):
    ids = [a.create(f"note {i}")[0] for i in range(5)]
    seen, cursor = [], 0
    while True:
        page = a.changes(cursor, limit=2).json()
        seen += [n["id"] for n in page["changes"]]
        cursor = page["cursor"]
        if not page["has_more"]:
            break
    assert seen == ids


def test_changes_returns_only_newer_and_an_edited_note_moves_to_the_end(a):
    n1, first = a.create("one")
    n2, second = a.create("two")
    a.put(n1, "one edited", base_rev=1)

    feed = a.changes(second["seq"]).json()
    assert [n["id"] for n in feed["changes"]] == [n1]
    assert a.changes(feed["cursor"]).json()["changes"] == []


def test_cursor_from_the_future_means_resync(a):
    a.create("x")
    assert a.changes(999).status_code == 410


def test_position_change_is_metadata_only(a):
    nid, n1 = a.create("body")
    r = a.move(nid, "b5")
    assert r.status_code == 200
    moved = r.json()
    assert moved["position"] == "b5" and moved["rev"] == n1["rev"] and moved["seq"] > n1["seq"]
    # a concurrent content edit based on rev 1 is still accepted
    assert a.put(nid, "body v2", base_rev=1).status_code == 200


# ------------------------------------------------------------ history

def test_history_snapshots_previous_version_once_per_window(a, monkeypatch):
    clock = {"now": note_service.utcnow()}
    monkeypatch.setattr(note_service, "utcnow", lambda: clock["now"])

    nid, _ = a.create("v1")
    a.put(nid, "v2", base_rev=1)
    a.put(nid, "v3", base_rev=2)  # same window: no extra snapshot
    assert [r["rev"] for r in a.revisions(nid).json()] == [1]

    clock["now"] += timedelta(seconds=settings.revision_coalesce_seconds + 1)
    a.put(nid, "v4", base_rev=3)
    assert sorted(r["rev"] for r in a.revisions(nid).json()) == [1, 3]

    rev_id = a.revisions(nid).json()[-1]["id"]
    detail = a.c.get(f"/v2/notes/{nid}/revisions/{rev_id}", headers=a.h).json()
    assert detail["content"] == "v1"


def test_big_shrink_is_always_snapshotted(a):
    big = "important line\n" * 40
    nid, _ = a.create(big)
    a.put(nid, "x", base_rev=1)  # user wipes the note within the coalesce window
    revs = a.revisions(nid).json()
    assert len(revs) == 1
    detail = a.c.get(f"/v2/notes/{nid}/revisions/{revs[0]['id']}", headers=a.h).json()
    assert detail["content"] == big


def test_history_is_capped(a, monkeypatch):
    monkeypatch.setattr(settings, "revision_keep", 3)
    clock = {"now": note_service.utcnow()}
    monkeypatch.setattr(note_service, "utcnow", lambda: clock["now"])
    nid, _ = a.create("v0")
    for i in range(1, 8):
        clock["now"] += timedelta(seconds=settings.revision_coalesce_seconds + 1)
        a.put(nid, f"v{i}", base_rev=i)
    assert len(a.revisions(nid).json()) == 3


# ------------------------------------------------------------ retention jobs

def _run(fn, now):
    db = SessionLocal()
    try:
        return fn(db, now)
    finally:
        db.close()


def test_purge_drops_content_history_and_signals_other_devices(a):
    nid, _ = a.create("secret words " * 30)
    a.put(nid, "tiny", base_rev=1)  # creates a history snapshot
    a.trash(nid, base_rev=2)
    before = a.get(nid).json()

    later = note_service.utcnow() + timedelta(days=settings.trash_retention_days + 1)
    assert _run(maintenance.purge_expired_trash, later) == 1

    n = a.get(nid).json()
    assert n["purged"] and n["content"] == "" and n["rev"] > before["rev"] and n["seq"] > before["seq"]
    assert a.revisions(nid).json() == []
    assert a.changes(before["seq"]).json()["changes"][0]["purged"] is True
    assert a.put(nid, "zombie edit", base_rev=n["rev"]).status_code == 409


def test_purge_leaves_recent_trash_and_live_notes(a):
    live, _ = a.create("live")
    trashed, _ = a.create("recently trashed")
    a.trash(trashed, base_rev=1)
    assert _run(maintenance.purge_expired_trash, note_service.utcnow()) == 0
    assert a.get(trashed).json()["content"] == "recently trashed"


def test_dropping_tombstones_forces_stale_cursors_to_resync(a):
    keep, k = a.create("stays")
    gone, _ = a.create("goes")
    stale_cursor = a.changes(0).json()["cursor"]  # a device that has seen both notes
    a.trash(gone, base_rev=1)

    t1 = note_service.utcnow() + timedelta(days=settings.trash_retention_days + 1)
    _run(maintenance.purge_expired_trash, t1)
    t2 = t1 + timedelta(days=settings.tombstone_retention_days + 1)
    assert _run(maintenance.drop_old_tombstones, t2) == 1

    assert a.get(gone).status_code == 404
    assert a.changes(stale_cursor).status_code == 410  # it could have missed the deletion
    fresh = a.changes(0)  # from scratch is always allowed
    assert fresh.status_code == 200 and [n["id"] for n in fresh.json()["changes"]] == [keep]


def test_old_history_is_thinned_but_newest_few_survive(a, monkeypatch):
    clock = {"now": note_service.utcnow()}
    monkeypatch.setattr(note_service, "utcnow", lambda: clock["now"])
    nid, _ = a.create("v0")
    for i in range(1, 9):
        clock["now"] += timedelta(seconds=settings.revision_coalesce_seconds + 1)
        a.put(nid, f"v{i}", base_rev=i)
    assert len(a.revisions(nid).json()) == 8

    far_future = clock["now"] + timedelta(days=settings.revision_retention_days + 5)
    _run(maintenance.prune_old_revisions, far_future)
    assert len(a.revisions(nid).json()) == settings.revision_keep_min


# ------------------------------------------------------------ image garbage collection

def upload(client, headers, image_id):
    r = client.put(f"/images/{image_id}", headers=headers, content=b"\x89PNG fake", )
    assert r.status_code == 204, r.text


def manifest(client, headers):
    return set(client.get("/images/manifest", headers=headers).json()["ids"])


def test_orphan_images_are_removed_but_anything_still_referenced_is_kept(client, alice, a):
    from app import maintenance
    for name in ("live", "trashed", "old_revision", "legacy", "orphan"):
        upload(client, alice, name)
    a.create("text ![](kqnote-image:live) more")
    tid, _ = a.create("![](kqnote-image:trashed)")
    a.trash(tid, base_rev=1)
    rid, _ = a.create("v1 [[image:old_revision]]")
    a.put(rid, "v2 with the picture removed " * 20, base_rev=1)  # v1 is kept as a history snapshot
    client.put("/notes/me", headers=alice, json={"content": "![](kqnote-image:legacy)", "base_version": 0, "device_id": "d"})

    later = note_service.utcnow() + timedelta(days=settings.image_gc_grace_days + 1)
    removed = _run(maintenance.gc_orphan_images, later)

    assert removed == 1
    assert manifest(client, alice) == {"live", "trashed", "old_revision", "legacy"}


def test_images_inside_the_grace_period_are_never_removed(client, alice):
    from app import maintenance
    upload(client, alice, "fresh")
    soon = note_service.utcnow() + timedelta(days=settings.image_gc_grace_days - 1)
    assert _run(maintenance.gc_orphan_images, soon) == 0
    assert manifest(client, alice) == {"fresh"}


def test_an_image_is_only_kept_alive_by_its_own_owners_notes(client, alice, bob, a, b):
    from app import maintenance
    upload(client, alice, "shared-name")
    upload(client, bob, "shared-name")
    b.create("![](kqnote-image:shared-name)")  # only bob's note uses it
    later = note_service.utcnow() + timedelta(days=settings.image_gc_grace_days + 1)
    assert _run(maintenance.gc_orphan_images, later) == 1
    assert manifest(client, alice) == set()
    assert manifest(client, bob) == {"shared-name"}


def test_purging_a_note_frees_its_images(client, alice, a):
    from app import maintenance
    upload(client, alice, "pic")
    nid, _ = a.create("![](kqnote-image:pic)")
    a.trash(nid, base_rev=1)
    purge(a, nid, 2)
    later = note_service.utcnow() + timedelta(days=settings.image_gc_grace_days + 1)
    assert _run(maintenance.gc_orphan_images, later) == 1


# ------------------------------------------------------------ legacy shim

def test_legacy_notes_me_endpoint_still_works_and_is_separate_from_v2(client, alice, a):
    r = client.get("/notes/me", headers=alice)
    assert r.status_code == 200 and r.json()["content"] == ""
    r = client.put("/notes/me", headers=alice,
                   json={"content": "old client text", "base_version": 0, "device_id": "d"})
    assert r.status_code == 200 and r.json()["version"] == 1

    assert a.changes(0).json()["changes"] == []  # v1 writes don't leak into v2
    nid, _ = a.create("v2 note")
    assert client.get("/notes/me", headers=alice).json()["content"] == "old client text"


# ------------------------------------------------------------ concurrency (Postgres only)

@pytest.mark.skipif(not IS_POSTGRES, reason="needs real row locks; set TEST_DATABASE_URL")
def test_concurrent_writers_exactly_one_wins(client, alice, a):
    from concurrent.futures import ThreadPoolExecutor

    nid, _ = a.create("base")

    def attempt(i):
        return a.put(nid, f"from device {i}", base_rev=1).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(attempt, range(8)))

    assert sorted(codes) == [200] + [409] * 7
    assert a.get(nid).json()["rev"] == 2


@pytest.mark.skipif(not IS_POSTGRES, reason="needs real row locks; set TEST_DATABASE_URL")
def test_concurrent_creates_get_unique_gapless_seqs(a):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: a.put(new_id(), f"n{i}", 0), range(24)))

    assert all(r.status_code == 200 for r in results)
    seqs = sorted(r.json()["seq"] for r in results)
    assert seqs == list(range(1, 25))
    assert len(a.changes(0, limit=500).json()["changes"]) == 24
