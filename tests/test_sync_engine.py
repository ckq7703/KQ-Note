import os
import random
import sqlite3
import time
import unittest
from unittest import mock

import requests

from tests.sync_harness import SyncCase


def texts(rows):
    return sorted(r["content"] for r in rows)


class FirstLoginTest(SyncCase):
    def test_local_notes_are_uploaded_and_a_second_device_receives_them(self):
        a = self.device("a", register=True)
        a.activate()
        a.store.save_note_by_id(a.store.get_active_note_id(), "# Shopping\nmilk")
        a.create("# Ideas\nbuild a rocket")

        status, events = a.sync()

        self.assertEqual(status["state"], "synced")
        self.assertEqual(status["pending"], 0)
        self.assertEqual(a.scope, self.email)  # account_ready switched the visible scope
        server_texts = texts(self.server_notes())
        self.assertIn("# Shopping\nmilk", server_texts)
        self.assertIn("# Ideas\nbuild a rocket", server_texts)

        b = self.device("b")
        b.sync()
        self.assertEqual(b.live_texts(), a.live_texts())

    def test_signing_in_on_a_device_with_the_same_text_does_not_duplicate_notes(self):
        a = self.device("a", register=True)
        a.create("# real note")
        a.sync()
        b = self.device("b")  # a fresh install starts with the same starter note as a did
        b.sync()
        b.sync()
        self.assertEqual(b.live_texts().count("# real note"), 1)
        self.assertEqual(len(self.server_notes()), 1)  # only the real note ever reached the server

    def test_untouched_starter_notes_are_never_uploaded(self):
        a = self.device("a", register=True)
        a.create("# Ghi chú mới\n\nNội dung ghi chú...")
        a.sync()
        a.sync()
        self.assertEqual(len(self.server_notes()), 0)
        self.assertEqual(a.engine.status()["pending"], 0)

    def test_different_local_notes_on_the_new_device_are_merged_in_not_lost(self):
        a = self.device("a", register=True)
        a.create("# from A")
        a.sync()
        b = self.device("b")
        b.create("# from B")
        b.sync()
        a.sync()
        for d in (a, b):
            self.assertIn("# from A", d.live_texts())
            self.assertIn("# from B", d.live_texts())
        self.assertEqual(a.live_texts(), b.live_texts())

    def test_logging_out_shows_the_local_notes_again_and_relogin_does_not_duplicate(self):
        a = self.device("a", register=True)
        a.create("# local only")
        a.sync()
        count = len(a.live())
        a.logout()
        self.assertIn("# local only", [r["content"] for r in a.rows(None)])
        a.login(self.email)
        a.sync()
        self.assertEqual(len(a.live()), count)
        self.assertEqual(len(self.server_notes()), count)

    def test_the_old_single_note_cloud_blob_is_imported_once_unless_already_a_note(self):
        reg = requests.post(f"{self.server.url}/auth/register", json={"email": self.email, "password": "password-123"})
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        requests.put(f"{self.server.url}/notes/me", headers=headers,
                     json={"content": "# Written by an old client\nkeep", "base_version": 0, "device_id": "old"})
        a = self.device("a")
        a.sync()
        self.assertIn("# Written by an old client\nkeep", a.live_texts())
        a.sync()  # second cycle must not import it again
        self.assertEqual(a.live_texts().count("# Written by an old client\nkeep"), 1)


class PropagationTest(SyncCase):
    def setUp(self):
        super().setUp()
        self.a = self.device("a", register=True)
        self.a.sync()
        self.b = self.device("b")
        self.b.sync()

    def note_id(self, device, text):
        return next(r["id"] for r in device.live() if r["content"] == text)

    def test_edits_trash_restore_and_order_propagate(self):
        a, b = self.a, self.b
        one = a.create("# one")
        two = a.create("# two")
        a.sync()
        b.sync()
        self.assertIn("# one", b.live_texts())

        a.edit(one, "# one edited")
        a.trash(two)
        a.sync()
        b.sync()
        self.assertIn("# one edited", b.live_texts())
        self.assertNotIn("# two", b.live_texts())
        self.assertIn("# two", [r["content"] for r in b.trashed()])

        self.assertTrue(a.restore(two))
        a.sync()
        b.sync()
        self.assertIn("# two", b.live_texts())

        order = [r["id"] for r in a.synced_live()]
        a.reorder(list(reversed(order)))
        a.sync()
        b.sync()
        self.assertEqual([r["id"] for r in b.synced_live()], list(reversed(order)))

    def test_a_note_is_created_on_b_and_reaches_a(self):
        self.b.create("# made on b")
        self.b.sync()
        self.a.sync()
        self.assertIn("# made on b", self.a.live_texts())

    def test_conflicting_edits_keep_both_texts(self):
        a, b = self.a, self.b
        nid = a.create("# shared\nline")
        a.sync()
        b.sync()

        a.edit(nid, "# shared\nline\nAAA typed on a")
        b.edit(nid, "# shared\nline\nBBB typed on b")
        a.sync()
        status, events = b.sync()

        self.assertTrue([p for k, p in events if k == "notes_changed" and p["conflicts"]])
        a.sync()
        b.sync()
        for d in (a, b):
            joined = d.all_text()
            self.assertIn("AAA typed on a", joined)
            self.assertIn("BBB typed on b", joined)
        self.assertEqual(a.live_texts(), b.live_texts())
        self.assertTrue([t for t in a.live_texts() if t.startswith("# [Xung đột]")])

    def test_edit_on_one_device_beats_delete_on_the_other(self):
        a, b = self.a, self.b
        nid = a.create("# precious")
        a.sync()
        b.sync()

        a.trash(nid)
        a.sync()
        b.edit(nid, "# precious\nstill working on it")  # b hasn't heard about the delete
        b.sync()
        a.sync()

        for d in (a, b):
            self.assertIn("# precious\nstill working on it", d.live_texts())

    def test_delete_on_both_devices_is_fine(self):
        a, b = self.a, self.b
        nid = a.create("# doomed")
        a.sync()
        b.sync()
        a.trash(nid)
        b.trash(nid)
        a.sync()
        b.sync()
        a.sync()
        self.assertNotIn("# doomed", a.live_texts())
        self.assertNotIn("# doomed", b.live_texts())

    def test_a_stale_delete_does_not_remove_a_newer_edit(self):
        a, b = self.a, self.b
        nid = a.create("# v1")
        a.sync()
        b.sync()
        a.edit(nid, "# v2 newer")
        a.sync()
        b.trash(nid)  # b deletes the version it still sees
        b.sync()
        a.sync()
        self.assertIn("# v2 newer", b.live_texts())
        self.assertIn("# v2 newer", a.live_texts())


class FailureTest(SyncCase):
    def setUp(self):
        super().setUp()
        self.a = self.device("a", register=True)
        self.a.sync()

    def test_offline_edits_are_kept_and_uploaded_when_the_network_returns(self):
        a = self.a
        a.session.offline = True
        nid = a.create("# written offline")
        status, _ = a.sync()
        self.assertEqual(status["state"], "offline")
        self.assertIn("# written offline", a.live_texts())
        self.assertNotIn("# written offline", texts(self.server_notes()))

        a.session.offline = False
        status, _ = a.sync()
        self.assertEqual(status["state"], "synced")
        self.assertIn("# written offline", texts(self.server_notes()))

    def test_a_lost_response_is_retried_without_duplicates_or_conflicts(self):
        a = self.a
        a.create("# only once")
        a.session.drop_response_when = lambda m, u: m == "PUT" and "/v2/notes/" in u
        status, _ = a.sync()  # the server applied it, but the answer never arrived
        self.assertEqual(status["state"], "offline")

        status, events = a.sync()

        self.assertEqual(status["state"], "synced")
        self.assertEqual(texts(self.server_notes()).count("# only once"), 1)
        self.assertFalse([p for k, p in events if k == "notes_changed" and p["conflicts"]])
        self.assertEqual(a.live_texts().count("# only once"), 1)

    def test_a_crash_between_the_server_saying_yes_and_us_recording_it(self):
        a = self.a
        a.create("# crash test")
        with mock.patch("app.sync.repo.record_server_note", side_effect=RuntimeError("power cut")):
            status, _ = a.sync()
        self.assertEqual(status["state"], "error")

        status, _ = a.sync()  # next launch: the same request is recognised, nothing doubles
        self.assertEqual(status["state"], "synced")
        self.assertEqual(texts(self.server_notes()).count("# crash test"), 1)
        self.assertEqual(a.live_texts().count("# crash test"), 1)

    def test_one_bad_note_does_not_block_the_others(self):
        a = self.a
        huge = a.create("x" * 1_100_000)  # over the server's 1 MB limit
        fine = a.create("# fine")
        status, _ = a.sync()
        self.assertEqual(status["state"], "error")
        self.assertIn("# fine", texts(self.server_notes()))

        a.edit(huge, "now small")
        status, _ = a.sync()
        self.assertEqual(status["state"], "synced")
        self.assertIn("now small", texts(self.server_notes()))

    def test_unsynced_work_survives_a_restart(self):
        a = self.a
        a.create("# before restart")
        a.session.offline = True
        a.sync()
        # a new process: fresh engine, same data directory
        from app.sync.engine import SyncEngine
        a.activate()
        a.engine = SyncEngine(self.server.url)
        a.session = type(a.session)(a.engine.client.session)
        a.engine.client.session = a.session
        status, _ = a.sync()
        self.assertEqual(status["state"], "synced")
        self.assertIn("# before restart", texts(self.server_notes()))

    def test_a_server_restored_from_an_older_backup_gets_the_missing_notes_back(self):
        a = self.a
        keep = a.create("# created before the backup")
        a.sync()
        # the server loses everything after some point: simulate by wiping its notes and rewinding seq
        self.server.query("DELETE FROM notes WHERE user_id = (SELECT id FROM users WHERE email = ?)", (self.email,))
        self.server.query("UPDATE user_sync SET seq = 0, tombstone_floor = 0 WHERE user_id = "
                          "(SELECT id FROM users WHERE email = ?)", (self.email,))
        a.create("# created after the backup")

        status, _ = a.sync()

        self.assertEqual(status["state"], "synced")
        server = texts(self.server_notes())
        self.assertIn("# created before the backup", server)
        self.assertIn("# created after the backup", server)
        self.assertEqual(len(a.synced_live()), len(self.server_notes()))
        self.assertIn(keep, [r["id"] for r in a.live()])

    def test_a_note_purged_on_the_server_while_edited_here_survives(self):
        a = self.a
        b = self.device("b")
        b.sync()
        nid = a.create("# will be purged")
        a.sync()
        b.sync()
        a.trash(nid)
        a.sync()
        self.server.run_backend_code(
            "import datetime\n"
            "from app.database import SessionLocal\n"
            "from app import maintenance\n"
            "db = SessionLocal()\n"
            "maintenance.purge_expired_trash(db, datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=61))\n")
        # b never saw the delete and has unsynced work on that note
        b.edit(nid, "# will be purged\nprecious unsynced work")
        status, _ = b.sync()

        self.assertEqual(status["state"], "synced")
        self.assertIn("# will be purged\nprecious unsynced work", b.live_texts())
        self.assertIn("# will be purged\nprecious unsynced work", texts(self.server_notes()))
        a.sync()
        self.assertNotIn("# will be purged", [t for t in a.live_texts() if "precious" not in t])

    def test_many_notes_arrive_across_several_pages(self):
        a = self.a
        for i in range(130):
            a.create(f"# note {i}")
        a.sync()
        b = self.device("b")
        with mock.patch("app.sync.engine.PAGE_SIZE", 40):
            b.sync()
        self.assertEqual(b.synced_texts(), a.synced_texts())
        self.assertGreaterEqual(len(b.synced_live()), 130)


class FuzzTest(SyncCase):
    """Random work on three devices with flaky networks. Whatever happens, every piece of text
    a user typed must still exist somewhere at the end, and the devices must agree."""

    def run_scenario(self, seed, steps=45):
        rng = random.Random(seed)
        devices = [self.device("d0", register=True), self.device("d1"), self.device("d2")]
        for d in devices:
            d.sync()
        typed = []
        counter = [0]

        def token():
            counter[0] += 1
            t = f"<t{seed}.{counter[0]}>"
            typed.append(t)
            return t

        for _ in range(steps):
            d = rng.choice(devices)
            action = rng.choice(["create", "edit", "edit", "trash", "restore", "reorder", "sync", "sync", "flip"])
            live = d.live()
            if action == "create":
                d.create(f"# note {token()}")
            elif action == "edit" and live:
                row = rng.choice(live)
                d.edit(row["id"], row["content"] + f"\n{token()}")
            elif action == "trash" and len(live) > 1:
                d.trash(rng.choice(live)["id"])
            elif action == "restore" and d.trashed():
                d.restore(rng.choice(d.trashed())["id"])
            elif action == "reorder" and len(live) > 1:
                ids = [r["id"] for r in live]
                rng.shuffle(ids)
                d.reorder(ids)
            elif action == "sync":
                d.sync()
            elif action == "flip":
                d.session.offline = not d.session.offline

        for d in devices:  # everyone comes back online and syncs until things settle
            d.session.offline = False
        for _ in range(4):
            for d in devices:
                d.sync()

        server = self.server_notes()
        server_text = "\n".join(r["content"] for r in server)
        for t in typed:
            self.assertIn(t, server_text, f"seed {seed}: {t} was typed but is nowhere on the server")

        expected = {r["id"]: (r["content"], r["deleted_at"] is not None) for r in server if r["purged_at"] is None}
        for d in devices:
            mine = {r["id"]: (r["content"], r["deleted_at"] is not None) for r in d.synced_rows()}
            self.assertEqual(mine, expected, f"seed {seed}: {d.name} disagrees with the server")
            starters = {"# Ghi chú mới", "# Ghi chú mới\n\nNội dung ghi chú...", ""}
            for r in d.unsynced_rows():  # anything not on the server must be untouched starter text
                self.assertIn(r["content"].strip(), starters, f"seed {seed}: {d.name} never uploaded {r['content']!r}")
        orders = [[r["id"] for r in d.synced_live()] for d in devices]
        self.assertEqual(orders[0], orders[1], f"seed {seed}: list order differs")
        self.assertEqual(orders[0], orders[2], f"seed {seed}: list order differs")
        self.assertTrue(all(d.engine.status()["pending"] == 0 for d in devices))

    def test_random_multi_device_work_never_loses_text_and_converges(self):
        # FUZZ_SEEDS=40 FUZZ_STEPS=60 for a longer soak; the defaults keep the normal run quick.
        first = int(os.environ.get("FUZZ_FIRST_SEED", "0"))
        for seed in range(first, first + int(os.environ.get("FUZZ_SEEDS", "6"))):
            with self.subTest(seed=seed):
                self.email = f"fuzz{seed}-{int(time.time() * 1000) % 100000}@example.com"
                self.run_scenario(seed, steps=int(os.environ.get("FUZZ_STEPS", "45")))


if __name__ == "__main__":
    unittest.main()
