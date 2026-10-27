"""
Tests for delivery receipts (ACK), the offline queue (store and forward), and how the
TCP layer reacts to broken or hostile input. All with real sockets on localhost.
"""

import os
import queue
import socket
import struct
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netchat import config, protocol
from netchat.logger import EventLog
from netchat.peer import Peer
from netchat.storage import Database

TEST_UDP_PORT = 50502        # different from the other test files and from the real app


def free_tcp_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until(condition, seconds=8):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.05)
    return False


class Node:
    """A Peer plus an inbox of received texts and a list of all events it reported."""

    def __init__(self, name, db_path=":memory:", tcp_port=None):
        self.inbox = queue.Queue()
        self.events = []
        self.db = Database(db_path, name)
        self.peer = Peer(name, tcp_port or free_tcp_port(), TEST_UDP_PORT, EventLog(name),
                         on_message=lambda s, t, ts: self.inbox.put((s, t)),
                         on_event=lambda kind, info: self.events.append((kind, info)),
                         db=self.db)

    def statuses(self, msg_id=None):
        return [info["status"] for kind, info in self.events
                if kind == "msg_status" and (msg_id is None or info["id"] == msg_id)]

    def start(self):
        self.peer.start()
        return self

    def stop(self):
        self.peer.stop()
        self.db.close()


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.nodes = []
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        for node in self.nodes:
            try:
                node.peer.stop()
            except Exception:
                pass
        time.sleep(0.2)
        try:
            self.tmp.cleanup()
        except OSError:
            pass                                  # sqlite file still open on Windows: not important

    def node(self, name, **kwargs):
        node = Node(name, **kwargs)
        self.nodes.append(node)
        return node

    def introduce(self, a, b):
        a.peer.table.add(b.peer.name, "127.0.0.1", b.peer.tcp_port)
        b.peer.table.add(a.peer.name, "127.0.0.1", a.peer.tcp_port)

    # ---- delivery receipts ---------------------------------------------
    def test_ack_marks_message_delivered(self):
        alice, bob = self.node("Alice").start(), self.node("Bob").start()
        self.introduce(alice, bob)
        msg_id = uuid.uuid4().hex
        self.assertEqual(alice.peer.deliver("Bob", msg_id, {"text": "hi", "ts": "10:00:00"}), "sent")
        self.assertEqual(bob.inbox.get(timeout=3), ("Alice", "hi"))
        self.assertTrue(wait_until(lambda: "delivered" in alice.statuses(msg_id)))
        self.assertEqual(alice.db.outbox_count(), 0)                 # ACKed messages leave the queue

    def test_missing_ack_becomes_failed_and_retry_works(self):
        old_timeout = config.ACK_TIMEOUT
        config.ACK_TIMEOUT = 1.0
        self.addCleanup(setattr, config, "ACK_TIMEOUT", old_timeout)
        alice, bob = self.node("Alice").start(), self.node("Bob").start()
        self.introduce(alice, bob)
        real_reply = bob.peer._reply
        bob.peer._reply = lambda *a, **k: None                       # Bob receives but never answers
        msg_id = uuid.uuid4().hex
        alice.peer.deliver("Bob", msg_id, {"text": "no answer", "ts": "10:00:00"})
        self.assertEqual(bob.inbox.get(timeout=3), ("Alice", "no answer"))
        self.assertTrue(wait_until(lambda: "failed" in alice.statuses(msg_id), 6), "never became failed")
        # the user clicks Retry: same id again. Bob now answers, and must NOT show it twice.
        bob.peer._reply = real_reply
        alice.peer.deliver("Bob", msg_id, {"text": "no answer", "ts": "10:00:00"})
        self.assertTrue(wait_until(lambda: "delivered" in alice.statuses(msg_id)))
        self.assertTrue(bob.inbox.empty(), "retry was delivered to the user twice")

    def test_duplicate_message_ids_are_ignored(self):
        alice, bob = self.node("Alice").start(), self.node("Bob").start()
        self.introduce(alice, bob)
        for _ in range(3):
            alice.peer.deliver("Bob", "same-id", {"text": "once", "ts": "10:00:00"})
        self.assertEqual(bob.inbox.get(timeout=3), ("Alice", "once"))
        self.assertTrue(wait_until(lambda: alice.statuses("same-id").count("delivered") >= 1))
        time.sleep(0.3)
        self.assertTrue(bob.inbox.empty())

    # ---- offline queue -------------------------------------------------
    def test_messages_for_an_offline_peer_are_queued_and_delivered_in_order(self):
        alice = self.node("Alice").start()
        bob = self.node("Bob")                                        # created but NOT started
        alice.peer.table.add("Bob", "127.0.0.1", bob.peer.tcp_port)  # Alice thinks Bob is there
        ids = []
        for text in ("first", "second", "third"):
            ids.append(uuid.uuid4().hex)
            self.assertEqual(alice.peer.deliver("Bob", ids[-1], {"text": text, "ts": "10:00:00"}), "queued")
        self.assertEqual(alice.db.outbox_count("Bob"), 3)
        self.assertTrue(all("queued" in alice.statuses(i) for i in ids))
        self.assertNotIn("Bob", alice.peer.peer_names())              # failed send removed the peer

        bob.start()                                                   # Bob comes back: his HELLO triggers delivery
        got = [bob.inbox.get(timeout=8)[1] for _ in range(3)]
        self.assertEqual(got, ["first", "second", "third"])
        self.assertTrue(wait_until(lambda: alice.db.outbox_count() == 0), "queue was not emptied by the ACKs")
        self.assertTrue(all("delivered" in alice.statuses(i) for i in ids))

    def test_queue_survives_a_restart(self):
        path = os.path.join(self.tmp.name, "alice.db")
        alice = self.node("Alice", db_path=path).start()
        bob = self.node("Bob")
        alice.peer.table.add("Bob", "127.0.0.1", bob.peer.tcp_port)
        alice.peer.deliver("Bob", uuid.uuid4().hex, {"text": "survives", "ts": "10:00:00"})
        alice.stop()                                                  # program closed with the message still waiting

        alice2 = self.node("Alice", db_path=path).start()             # program started again, same file
        self.assertEqual(alice2.db.outbox_count("Bob"), 1)
        bob.start()
        self.assertEqual(bob.inbox.get(timeout=8), ("Alice", "survives"))
        self.assertTrue(wait_until(lambda: alice2.db.outbox_count() == 0))

    def test_message_in_flight_when_the_connection_breaks_is_sent_again(self):
        alice, bob = self.node("Alice").start(), self.node("Bob").start()
        self.introduce(alice, bob)
        alice.peer.send_message("Bob", "warm up")                     # opens the connection
        bob.inbox.get(timeout=3)
        for i in range(5):
            alice.peer.send_message("Bob", "msg %d" % i)
            conn = alice.peer.connections.get("Bob")
            if conn is not None:
                conn.close()                                          # kill it right after sending
        got = set()
        while True:
            try:
                got.add(bob.inbox.get(timeout=6)[1])
            except queue.Empty:
                break
        self.assertEqual(got, {"msg %d" % i for i in range(5)})       # nothing lost, and no repeats shown

    # ---- hostile / broken input on the TCP port ------------------------
    def raw_connection(self, node):
        sock = socket.create_connection(("127.0.0.1", node.peer.tcp_port), timeout=3)
        self.addCleanup(sock.close)
        return sock

    def assert_server_hangs_up(self, sock):
        sock.settimeout(4)
        data = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    return data                                      # closed cleanly: what we wanted
                data += chunk
        except socket.timeout:
            self.fail("server did not close the connection")
        except ConnectionError:
            return data

    def assert_peer_still_works(self, bob):
        alice = self.node("Alice%d" % len(self.nodes)).start()
        self.introduce(alice, bob)
        self.assertTrue(alice.peer.send_message("Bob", "still alive"))
        self.assertEqual(bob.inbox.get(timeout=3)[1], "still alive")

    def test_wrong_protocol_version_is_rejected_cleanly(self):
        bob = self.node("Bob").start()
        sock = self.raw_connection(bob)
        body = b'{"v":2,"t":"HI","name":"Mallory","pass":false}'
        sock.sendall(struct.pack(">I", len(body)) + body)
        reply = self.assert_server_hangs_up(sock)
        frame = protocol.read_frame(__import__("io").BytesIO(reply).read)   # Bob explains why
        self.assertEqual((frame.type, frame.get("code")), ("ERROR", "version"))
        self.assert_peer_still_works(bob)

    def test_garbage_and_oversized_frames_are_dropped(self):
        bob = self.node("Bob").start()
        for attack in (b"GET / HTTP/1.1\r\n\r\n",                         # not our protocol at all
                       struct.pack(">I", 2 * 1024 ** 3),                  # announces a 2 GB header
                       struct.pack(">I", 5) + b"{bad}",                   # bad JSON
                       struct.pack(">I", 0)):
            sock = self.raw_connection(bob)
            sock.sendall(attack)
            self.assert_server_hangs_up(sock)
        self.assert_peer_still_works(bob)

    def test_connection_that_never_says_hello_is_closed(self):
        old = config.HANDSHAKE_TIMEOUT
        config.HANDSHAKE_TIMEOUT = 1.0
        self.addCleanup(setattr, config, "HANDSHAKE_TIMEOUT", old)
        bob = self.node("Bob").start()
        sock = self.raw_connection(bob)                                   # connect and say nothing
        self.assert_server_hangs_up(sock)

    def test_first_frame_must_be_hi(self):
        bob = self.node("Bob").start()
        sock = self.raw_connection(bob)
        sock.sendall(protocol.encode_frame("MSG", {"id": "1", "text": "sneaky", "ts": "10:00:00"}))
        self.assert_server_hangs_up(sock)
        self.assertTrue(bob.inbox.empty())

    # ---- shutdown --------------------------------------------------------
    def test_stop_leaves_no_running_threads(self):
        alice, bob = self.node("Alice").start(), self.node("Bob").start()
        self.introduce(alice, bob)
        alice.peer.send_message("Bob", "hello")
        bob.inbox.get(timeout=3)
        alice.peer.send_typing("Bob")
        time.sleep(0.3)
        started = list(alice.peer.threads) + list(bob.peer.threads)
        started += alice.peer.discovery.threads + bob.peer.discovery.threads
        self.assertGreater(len(started), 4)
        alice.stop()
        bob.stop()
        still_running = [t for t in started if t.is_alive()]
        self.assertEqual(still_running, [])


if __name__ == "__main__":
    unittest.main()
