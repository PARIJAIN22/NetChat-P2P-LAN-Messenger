"""
File transfer tests: two real peers on localhost send real files to each other.
"""

import hashlib
import os
import queue
import socket
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netchat import config, filetransfer, protocol
from netchat.logger import EventLog
from netchat.peer import Peer
from netchat.storage import Database

TEST_UDP_PORT = 50503


def free_tcp_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until(condition, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


def sha256_of(path):
    return filetransfer.file_sha256(path)


class Node:
    def __init__(self, name, download_dir):
        self.inbox = queue.Queue()
        self.events = []
        self.offers = queue.Queue()
        self.db = Database(":memory:", name)
        self.download_dir = download_dir

        def on_event(kind, info):
            self.events.append((kind, info))
            if kind == "file_offer":
                self.offers.put(info)
        self.peer = Peer(name, free_tcp_port(), TEST_UDP_PORT, EventLog(name),
                         on_message=lambda s, t, ts: self.inbox.put((s, t)), on_event=on_event,
                         db=self.db, download_dir=download_dir)

    def states(self, fid):
        return [i["state"] for k, i in self.events if k == "file_state" and i["fid"] == fid]

    def last_state(self, fid):
        states = self.states(fid)
        return states[-1] if states else None

    def reason(self, fid):
        reasons = [i["reason"] for k, i in self.events if k == "file_state" and i["fid"] == fid and i["reason"]]
        return reasons[-1] if reasons else ""

    def progress(self, fid):
        return [i for k, i in self.events if k == "file_progress" and i["fid"] == fid]


class FileTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src")
        os.makedirs(self.src)
        self.alice = Node("Alice", os.path.join(self.tmp.name, "alice_downloads"))
        self.bob = Node("Bob", os.path.join(self.tmp.name, "bob_downloads"))
        self.alice.peer.start()
        self.bob.peer.start()
        self.alice.peer.table.add("Bob", "127.0.0.1", self.bob.peer.tcp_port)
        self.bob.peer.table.add("Alice", "127.0.0.1", self.alice.peer.tcp_port)

    def tearDown(self):
        self.alice.peer.stop()
        self.bob.peer.stop()
        self.alice.db.close()
        self.bob.db.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def make_file(self, name, size, pattern=None):
        path = os.path.join(self.src, name)
        with open(path, "wb") as f:
            remaining = size
            while remaining:
                n = min(remaining, 1024 * 1024)
                f.write(pattern * n if pattern else os.urandom(n))
                remaining -= n
        return path

    def send(self, path, accept=True):
        fid = self.alice.peer.files.offer("Bob", path)
        offer = self.bob.offers.get(timeout=10)
        self.assertEqual(offer["fid"], fid)
        if accept:
            return fid, offer, self.bob.peer.files.accept(fid)
        self.bob.peer.files.decline(fid)
        return fid, offer, None

    # ---- the main test ----------------------------------------------------
    def test_10_mb_random_file_arrives_identical(self):
        path = self.make_file("random.bin", 10 * 1024 * 1024)
        fid, offer, saved = self.send(path)
        self.assertEqual(offer["name"], "random.bin")
        self.assertEqual(offer["size"], 10 * 1024 * 1024)
        self.assertEqual(offer["sha256"], sha256_of(path))
        self.assertTrue(wait_until(lambda: self.bob.last_state(fid) == "verified"), self.bob.states(fid))
        self.assertTrue(wait_until(lambda: self.alice.last_state(fid) == "verified"), self.alice.states(fid))
        self.assertEqual(sha256_of(saved), sha256_of(path))                 # byte for byte identical
        self.assertEqual(os.path.getsize(saved), 10 * 1024 * 1024)
        self.assertEqual(os.path.dirname(saved), self.bob.download_dir)
        # the sender went through hashing -> offered -> sending -> (verifying) -> verified
        self.assertEqual(self.alice.states(fid)[:3], ["hashing", "offered", "sending"])
        self.assertEqual(self.bob.states(fid), ["receiving", "verified"])
        # progress was reported on both sides and ended at 100 %
        self.assertEqual(self.alice.progress(fid)[-1]["done"], 10 * 1024 * 1024)
        self.assertEqual(self.bob.progress(fid)[-1]["done"], 10 * 1024 * 1024)
        self.assertGreater(max(p["speed"] for p in self.bob.progress(fid)), 0)

    def test_empty_file(self):
        path = self.make_file("empty.txt", 0)
        fid, _, saved = self.send(path)
        self.assertTrue(wait_until(lambda: self.bob.last_state(fid) == "verified"))
        self.assertEqual(os.path.getsize(saved), 0)

    def test_chunks_are_64_kb(self):
        path = self.make_file("chunks.bin", 200 * 1024)
        seen = []
        real_on_frame = self.bob.peer.files.on_frame
        self.bob.peer.files.on_frame = lambda conn, frame: (seen.append(len(frame.data)) if frame.type == "FILE_CHUNK" else None, real_on_frame(conn, frame))
        fid, _, _ = self.send(path)
        self.assertTrue(wait_until(lambda: self.bob.last_state(fid) == "verified"))
        self.assertEqual(seen, [65536, 65536, 65536, 200 * 1024 - 3 * 65536])

    # ---- accept / decline / cancel ------------------------------------------
    def test_decline_sends_nothing(self):
        path = self.make_file("no.bin", 100000)
        fid, _, saved = self.send(path, accept=False)
        self.assertIsNone(saved)
        self.assertTrue(wait_until(lambda: self.alice.last_state(fid) == "declined"), self.alice.states(fid))
        self.assertEqual(self.bob.last_state(fid), "declined")
        self.assertFalse(os.path.exists(self.bob.download_dir) and os.listdir(self.bob.download_dir))

    def test_sender_can_cancel_and_partial_file_is_deleted(self):
        self.alice.peer.files.chunk_delay = 0.01                  # make it slow enough to cancel
        path = self.make_file("big.bin", 20 * 1024 * 1024)
        fid, _, saved = self.send(path)
        self.assertTrue(wait_until(lambda: self.bob.progress(fid) and self.bob.progress(fid)[-1]["done"] > 0))
        self.alice.peer.files.cancel(fid)
        self.assertTrue(wait_until(lambda: self.bob.last_state(fid) == "cancelled"), self.bob.states(fid))
        self.assertEqual(self.alice.last_state(fid), "cancelled")
        self.assertFalse(os.path.exists(saved), "partial file was not deleted")

    def test_receiver_can_cancel(self):
        self.alice.peer.files.chunk_delay = 0.01
        path = self.make_file("big.bin", 20 * 1024 * 1024)
        fid, _, saved = self.send(path)
        self.assertTrue(wait_until(lambda: self.bob.progress(fid)))
        self.bob.peer.files.cancel(fid)
        self.assertTrue(wait_until(lambda: self.alice.last_state(fid) == "cancelled"), self.alice.states(fid))
        self.assertFalse(os.path.exists(saved))

    # ---- chat is not blocked ---------------------------------------------------
    def test_chat_messages_arrive_while_a_file_is_transferring(self):
        self.alice.peer.files.chunk_delay = 0.005                 # ~ 4 s for 50 MB of chunks
        path = self.make_file("long.bin", 50 * 1024 * 1024, pattern=b"x")
        fid, _, _ = self.send(path)
        self.assertTrue(wait_until(lambda: self.bob.progress(fid) and self.bob.progress(fid)[-1]["done"] > 1024 * 1024))
        started = time.time()
        msg_id = uuid.uuid4().hex
        self.alice.peer.deliver("Bob", msg_id, {"text": "chat during transfer", "ts": "10:00:00"})
        self.assertEqual(self.bob.inbox.get(timeout=3), ("Alice", "chat during transfer"))
        self.assertTrue(wait_until(lambda: any(k == "msg_status" and i["id"] == msg_id and i["status"] == "delivered"
                                               for k, i in self.alice.events), 3), "no ACK during transfer")
        latency = time.time() - started
        self.assertNotEqual(self.bob.last_state(fid), "verified", "transfer finished before the chat message")
        self.assertLess(latency, 1.5)
        self.assertTrue(wait_until(lambda: self.bob.last_state(fid) == "verified", 30))

    # ---- integrity and safety ------------------------------------------------------
    def test_wrong_checksum_is_detected_and_file_deleted(self):
        path = self.make_file("tampered.bin", 300000)
        real = filetransfer.file_sha256
        filetransfer.file_sha256 = lambda p, stop=None: "0" * 64        # the sender "lies" about the hash
        self.addCleanup(setattr, filetransfer, "file_sha256", real)
        fid, _, saved = self.send(path)
        self.assertTrue(wait_until(lambda: self.bob.last_state(fid) == "failed"), self.bob.states(fid))
        self.assertIn("SHA-256", self.bob.reason(fid))
        self.assertTrue(wait_until(lambda: self.alice.last_state(fid) == "failed"))
        self.assertFalse(os.path.exists(saved), "a corrupt file was kept")

    def test_file_names_cannot_escape_the_download_folder(self):
        for evil in ("../../evil.txt", "..\\..\\evil.txt", "C:\\Windows\\evil.txt", "/etc/passwd", "a:b*c?.txt",
                     "CON", "...", ""):
            safe = filetransfer.sanitize_filename(evil)
            self.assertEqual(os.path.basename(safe), safe, evil)
            self.assertNotIn("/", safe)
            self.assertNotIn("\\", safe)
            self.assertTrue(safe.strip(" ."), evil)
            self.assertNotEqual(safe.upper(), "CON")
        self.assertEqual(filetransfer.sanitize_filename("report.pdf"), "report.pdf")

    def test_existing_files_are_never_overwritten(self):
        os.makedirs(self.bob.download_dir)
        existing = os.path.join(self.bob.download_dir, "same.txt")
        with open(existing, "wb") as f:
            f.write(b"precious")
        path = self.make_file("same.txt", 1000)
        fid, _, saved = self.send(path)
        self.assertTrue(wait_until(lambda: self.bob.last_state(fid) == "verified"))
        self.assertTrue(saved.endswith("same (1).txt"))
        with open(existing, "rb") as f:
            self.assertEqual(f.read(), b"precious")

    def test_malicious_chunks_are_rejected(self):
        # A raw peer that handshakes properly, then lies: wrong sequence number, or more data than offered.
        from netchat.connection import Connection
        for lie in ("bad seq", "too much"):
            sock = socket.create_connection(("127.0.0.1", self.bob.peer.tcp_port), timeout=3)
            conn = Connection(sock, ("127.0.0.1", 0), outgoing=True)
            stopping = __import__("threading").Event()
            conn.send("HI", {"name": "Mallory", "pass": False})
            self.assertEqual(conn.read_frame(stopping, time.time() + 3).type, "HI")
            fid = uuid.uuid4().hex
            conn.send("FILE_OFFER", {"fid": fid, "name": "x.bin", "size": 100, "sha256": "a" * 64})
            offer = self.bob.offers.get(timeout=3)
            self.bob.peer.files.accept(offer["fid"])
            self.assertEqual(conn.read_frame(stopping, time.time() + 3).type, "FILE_ACCEPT")
            if lie == "bad seq":
                conn.send("FILE_CHUNK", {"fid": fid, "seq": 7}, b"x" * 10)
            else:
                conn.send("FILE_CHUNK", {"fid": fid, "seq": 0}, b"x" * 101)
            self.assertTrue(wait_until(lambda: self.bob.last_state(fid) == "failed", 5), lie)
            self.assertEqual(conn.read_frame(stopping, time.time() + 3).type, "FILE_CANCEL")
            self.assertFalse(os.path.exists(os.path.join(self.bob.download_dir, "x.bin")), lie)
            conn.close()

    def test_absurd_offers_are_ignored(self):
        from netchat.connection import Connection
        sock = socket.create_connection(("127.0.0.1", self.bob.peer.tcp_port), timeout=3)
        conn = Connection(sock, ("127.0.0.1", 0), outgoing=True)
        stopping = __import__("threading").Event()
        conn.send("HI", {"name": "Mallory", "pass": False})
        conn.read_frame(stopping, time.time() + 3)
        for fields in ({"fid": "a", "name": "x", "size": filetransfer.MAX_FILE_BYTES + 1, "sha256": "a" * 64},
                       {"fid": "b", "name": "x", "size": -5, "sha256": "a" * 64},
                       {"fid": "c", "name": "x", "size": 5, "sha256": "not hex"},
                       {"fid": "d", "name": "", "size": 5, "sha256": "a" * 64},
                       {"fid": 5, "name": "x", "size": 5, "sha256": "a" * 64}):
            conn.send("FILE_OFFER", fields)
        time.sleep(0.5)
        self.assertTrue(self.bob.offers.empty())
        conn.close()

    def test_connection_loss_interrupts_and_the_transfer_resumes(self):
        self.alice.peer.files.chunk_delay = 0.01
        path = self.make_file("big.bin", 20 * 1024 * 1024)
        fid, _, saved = self.send(path)
        self.assertTrue(wait_until(lambda: self.bob.progress(fid)))
        self.alice.peer.connections["Bob"].close()                     # the network "breaks"
        self.assertTrue(wait_until(lambda: "interrupted" in self.bob.states(fid), 8), self.bob.states(fid))
        self.assertTrue(wait_until(lambda: self.bob.last_state(fid) == "verified", 30), self.bob.states(fid))
        self.assertTrue(wait_until(lambda: self.alice.last_state(fid) == "verified", 8), self.alice.states(fid))
        self.assertEqual(sha256_of(saved), sha256_of(path))

    def test_connection_loss_with_an_older_sender_fails_and_deletes_the_partial_file(self):
        # An older NetChat offers without "resume": the old behaviour (fail, delete) must stay.
        from netchat.connection import Connection
        sock = socket.create_connection(("127.0.0.1", self.bob.peer.tcp_port), timeout=3)
        conn = Connection(sock, ("127.0.0.1", 0), outgoing=True)
        stopping = __import__("threading").Event()
        conn.send("HI", {"name": "Oldie", "pass": False})
        conn.read_frame(stopping, time.time() + 3)
        fid = uuid.uuid4().hex
        conn.send("FILE_OFFER", {"fid": fid, "name": "old.bin", "size": 1000, "sha256": "a" * 64})
        saved = self.bob.peer.files.accept(self.bob.offers.get(timeout=3)["fid"])
        accept = conn.read_frame(stopping, time.time() + 3)
        self.assertEqual((accept.type, accept.get("offset")), ("FILE_ACCEPT", None))   # no resume fields
        conn.send("FILE_CHUNK", {"fid": fid, "seq": 0}, b"x" * 100)
        time.sleep(0.2)
        conn.close()
        self.assertTrue(wait_until(lambda: self.bob.last_state(fid) == "failed", 8), self.bob.states(fid))
        self.assertFalse(os.path.exists(saved))

if __name__ == "__main__":
    unittest.main()
