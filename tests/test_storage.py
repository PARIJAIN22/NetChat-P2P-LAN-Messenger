"""
The sqlite database: history, search, clearing, previews, the outbox order and groups.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netchat.storage import Database


def message(db, mid, chat="u:Bob", direction="out", text="hi", ts=None, **extra):
    return db.add_message(dict(id=mid, chat=chat, direction=direction, sender="Asha" if direction == "out" else "Bob",
                               text=text, ts=ts if ts is not None else time.time(), **extra))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:", "Asha")

    def test_messages_come_back_oldest_first(self):
        for i, text in enumerate(["one", "two", "three"]):
            message(self.db, "m%d" % i, text=text, ts=100 + i)
        self.assertEqual([m["text"] for m in self.db.messages("u:Bob")], ["one", "two", "three"])

    def test_limit_keeps_the_newest(self):
        for i in range(10):
            message(self.db, "m%d" % i, text=str(i), ts=100 + i)
        self.assertEqual([m["text"] for m in self.db.messages("u:Bob", limit=3)], ["7", "8", "9"])

    def test_duplicate_id_is_ignored(self):
        self.assertTrue(message(self.db, "same", text="first"))
        self.assertFalse(message(self.db, "same", text="second"))
        self.assertEqual([m["text"] for m in self.db.messages("u:Bob")], ["first"])

    def test_chats_do_not_mix(self):
        message(self.db, "a", chat="u:Bob", text="to Bob")
        message(self.db, "b", chat="u:Meera", text="to Meera")
        self.assertEqual([m["text"] for m in self.db.messages("u:Meera")], ["to Meera"])

    def test_owners_do_not_mix(self):
        other = Database(os.path.join(tempfile.mkdtemp(), "x.db"), "Rahul")
        other.close()
        path = os.path.join(tempfile.mkdtemp(), "shared.db")
        asha, rahul = Database(path, "Asha"), Database(path, "Rahul")
        message(asha, "m1", text="asha's")
        self.assertTrue(message(rahul, "m1", text="rahul's"))                  # same id, different owner: both kept
        self.assertEqual([m["text"] for m in asha.messages("u:Bob")], ["asha's"])
        self.assertEqual([m["text"] for m in rahul.messages("u:Bob")], ["rahul's"])
        asha.close(); rahul.close()

    def test_search_is_case_insensitive_and_ignores_system_lines(self):
        message(self.db, "a", text="Meeting at NOON")
        message(self.db, "b", text="lunch?")
        message(self.db, "c", direction="system", text="Bob joined noon")
        self.assertEqual([m["text"] for m in self.db.messages("u:Bob", search="noon")], ["Meeting at NOON"])

    def test_search_treats_percent_and_underscore_literally(self):
        message(self.db, "a", text="100% sure")
        message(self.db, "b", text="a_b")
        message(self.db, "c", text="axb")
        self.assertEqual([m["text"] for m in self.db.messages("u:Bob", search="%")], ["100% sure"])
        self.assertEqual([m["text"] for m in self.db.messages("u:Bob", search="a_b")], ["a_b"])

    def test_unicode_and_newlines_survive(self):
        text = "héllo 你好 \U0001F600\nsecond line"
        message(self.db, "u", text=text)
        self.assertEqual(self.db.messages("u:Bob")[0]["text"], text)

    def test_update_delete_and_clear(self):
        message(self.db, "a", text="x", status="sending", extra={"states": {"Bob": "sending"}})
        self.db.update_message("a", status="delivered", acked=1, extra={"states": {"Bob": "delivered"}})
        row = self.db.get_message("a")
        self.assertEqual((row["status"], row["acked"], row["extra"]), ("delivered", 1, {"states": {"Bob": "delivered"}}))
        message(self.db, "b", text="y")
        self.db.delete_message("a")
        self.assertIsNone(self.db.get_message("a"))
        self.db.clear_chat("u:Bob")
        self.assertEqual(self.db.messages("u:Bob"), [])

    def test_summaries_give_the_newest_message_of_each_chat(self):
        message(self.db, "a", chat="u:Bob", direction="in", text="old", ts=1)
        message(self.db, "b", chat="u:Bob", direction="out", text="new", ts=2)
        message(self.db, "c", chat="u:Meera", direction="in", text="hello", ts=3)
        message(self.db, "d", chat="u:Meera", direction="system", text="Meera left", ts=4)       # not a real message
        message(self.db, "e", chat="g:team", direction="in", text="standup", ts=5)
        message(self.db, "f", chat="u:Cara", direction="out", text="file", ts=6, kind="file", extra={"name": "a.png"})
        s = self.db.chat_summaries()
        self.assertEqual(s["u:Bob"], {"ts": 2, "preview": "You: new"})
        self.assertEqual(s["u:Meera"], {"ts": 3, "preview": "hello"})
        self.assertEqual(s["g:team"]["preview"], "Bob: standup")                                  # in groups: who said it
        self.assertEqual(s["u:Cara"]["preview"], "You: File: a.png")
        self.assertEqual(self.db.chats_with_messages(), {"u:Bob", "u:Meera", "g:team", "u:Cara"})

    def test_after_a_restart_nothing_is_still_sending(self):
        message(self.db, "q", status="sending")
        message(self.db, "r", status="sending")
        message(self.db, "t", chat="u:C", kind="file", extra={"name": "f", "state": "receiving"}, status="delivered")
        self.db.outbox_add("q", "Bob", {"text": "x"})                                             # still queued for delivery
        self.db.fail_stale_sending()
        self.assertEqual(self.db.get_message("q")["status"], "queued")
        self.assertEqual(self.db.get_message("r")["status"], "failed")
        self.assertEqual(self.db.get_message("t")["extra"]["state"], "failed")


class OutboxAndGroupTests(unittest.TestCase):
    def test_outbox_keeps_the_order_and_survives_reopening(self):
        path = os.path.join(tempfile.mkdtemp(), "q.db")
        db = Database(path, "Asha")
        for i in range(5):
            db.outbox_add("id%d" % i, "Bob", {"text": "m%d" % i})
        db.outbox_add("other", "Meera", {"text": "x"})
        db.close()
        db = Database(path, "Asha")
        self.assertEqual([i for i, _ in db.outbox_for("Bob")], ["id0", "id1", "id2", "id3", "id4"])
        self.assertEqual(db.outbox_count("Bob"), 5)
        self.assertEqual(db.outbox_count(), 6)
        self.assertTrue(db.outbox_remove("id2", "Bob"))
        self.assertFalse(db.outbox_remove("id2", "Bob"))
        self.assertEqual([i for i, _ in db.outbox_for("Bob")], ["id0", "id1", "id3", "id4"])
        db.close()

    def test_renaming_the_user_keeps_history_queue_and_groups(self):
        path = os.path.join(tempfile.mkdtemp(), "r.db")
        db = Database(path, "Asha")
        message(db, "m1", text="old chat")
        db.outbox_add("m1", "Bob", {"text": "x"})
        db.add_group("g1", "Team", ["Asha", "Bob"])
        db.rename_owner("AshaNew")
        self.assertEqual([m["text"] for m in db.messages("u:Bob")], ["old chat"])
        db.close()
        db = Database(path, "AshaNew")                                       # the program restarted under the new name
        self.assertEqual([m["text"] for m in db.messages("u:Bob")], ["old chat"])
        self.assertEqual(db.outbox_count("Bob"), 1)
        self.assertEqual(len(db.groups()), 1)
        self.assertEqual(Database(path, "Asha").messages("u:Bob"), [])        # nothing left under the old name

    def test_groups(self):
        db = Database(":memory:", "Asha")
        self.assertTrue(db.add_group("g1", "Study", ["Asha", "Bob"]))
        self.assertFalse(db.add_group("g1", "Renamed", ["Asha"]))                                 # first definition wins
        self.assertEqual(db.groups(), [{"id": "g1", "name": "Study", "members": ["Asha", "Bob"]}])


if __name__ == "__main__":
    unittest.main()
