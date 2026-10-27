"""
Stage 2: resumable file transfers (interruption, resume, corruption, restart), automatic
reconnection with bounded retries, and connecting to a peer by IP address.

Every node has its own UDP port, so discovery never interferes: the tests decide who knows whom.
Databases and identity keys are files, so a node can be "restarted" with the same state.
"""

import os
import queue
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netchat import config, filetransfer
from netchat.logger import EventLog
from netchat.peer import Peer
from netchat.storage import Database


def free_port(kind=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, kind)
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


class Node:
    """One NetChat instance whose state lives in `folder` (so it can be restarted)."""

    def __init__(self, name, folder, tcp_port=None):
        os.makedirs(folder, exist_ok=True)
        self.name, self.folder = name, folder
        self.tcp_port = tcp_port or free_port()
        self.start()

    def start(self):
        self.events, self.lines, self.offers, self.inbox = [], [], queue.Queue(), queue.Queue()
        log = EventLog(self.name)
        log.add_listener(self.lines.append)
        self.db = Database(os.path.join(self.folder, "netchat.db"), self.name)

        def on_event(kind, info):
            self.events.append((kind, info))
            if kind == "file_offer":
                self.offers.put(info)
        self.peer = Peer(self.name, self.tcp_port, free_port(socket.SOCK_DGRAM), log, on_event=on_event,
                         on_message=lambda s, t, ts: self.inbox.put((s, t)), db=self.db,
                         download_dir=os.path.join(self.folder, "downloads"),
                         identity_path=os.path.join(self.folder, "identity.key"))
        self.peer.start()

    def stop(self):
        self.peer.stop()
        self.db.close()

    def states(self, fid):
        return [i["state"] for k, i in self.events if k == "file_state" and i["fid"] == fid]

    def reasons(self, fid):
        return [i["reason"] for k, i in self.events if k == "file_state" and i["fid"] == fid]

    def done(self, fid):
        progress = [i["done"] for k, i in self.events if k == "file_progress" and i["fid"] == fid]
        return progress[-1] if progress else 0


class StageTwoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.nodes = []
        self.saved_delays = config.RECONNECT_DELAYS, config.RESUME_DELAYS
        config.RESUME_DELAYS = (0.2, 0.5, 1, 2)

    def tearDown(self):
        for node in self.nodes:
            node.stop()
        config.RECONNECT_DELAYS, config.RESUME_DELAYS = self.saved_delays
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def node(self, name, **kwargs):
        node = Node(name, os.path.join(self.tmp.name, name), **kwargs)
        self.nodes.append(node)
        return node

    def restart(self, node):
        node.stop()
        node.start()

    @staticmethod
    def introduce(a, b):
        a.peer.table.add(b.name, "127.0.0.1", b.tcp_port)
        b.peer.table.add(a.name, "127.0.0.1", a.tcp_port)

    def make_file(self, size):
        path = os.path.join(self.tmp.name, "source.bin")
        with open(path, "wb") as f:
            f.write(os.urandom(size))
        return path


class ResumeTests(StageTwoBase):
    SIZE = 6 * 1024 * 1024

    def start_transfer(self, alice, bob, delay=0.01):
        alice.peer.files.chunk_delay = delay
        path = self.make_file(self.SIZE)
        fid = alice.peer.files.offer("Bob", path)
        self.assertEqual(bob.offers.get(timeout=10)["fid"], fid)
        saved = bob.peer.files.accept(fid)
        return fid, path, saved

    def assert_identical(self, a, b):
        self.assertEqual(filetransfer.file_sha256(a), filetransfer.file_sha256(b))

    def test_interrupted_transfer_resumes_from_where_it_stopped(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        self.introduce(alice, bob)
        fid, path, saved = self.start_transfer(alice, bob)
        self.assertTrue(wait_until(lambda: bob.done(fid) > 2 * 1024 * 1024))
        # progress is persisted while the transfer runs (on both sides)
        self.assertTrue(wait_until(lambda: any(r["fid"] == fid and r["done"] > 0 for r in bob.db.saved_transfers())))
        self.assertTrue(any(r["fid"] == fid for r in alice.db.saved_transfers()))
        alice.peer.connections["Bob"].close()                                   # the network breaks
        self.assertTrue(wait_until(lambda: bob.states(fid)[-1:] == ["verified"], 30), bob.states(fid))
        self.assertTrue(wait_until(lambda: alice.states(fid)[-1:] == ["verified"]), alice.states(fid))
        self.assertIn("interrupted", alice.states(fid))
        resumed_at = [int(r.split()[2]) for r in bob.reasons(fid) if r.startswith("resuming at")]
        self.assertTrue(resumed_at and resumed_at[0] >= 2 * 1024 * 1024, bob.reasons(fid))   # not from 0
        self.assertEqual(resumed_at[0] % config.FILE_CHUNK_SIZE, 0)
        self.assert_identical(saved, path)
        self.assertEqual(bob.db.saved_transfers(), [])                          # finished: forgotten
        self.assertEqual(alice.db.saved_transfers(), [])

    def test_corrupted_partial_file_is_detected_and_the_transfer_starts_again(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        self.introduce(alice, bob)
        fid, path, saved = self.start_transfer(alice, bob)
        self.assertTrue(wait_until(lambda: bob.done(fid) > 1024 * 1024))
        bob.peer.stop()                                                         # receiver closes NetChat
        self.assertTrue(os.path.exists(saved), "the partial file must be kept for the resume")
        with open(saved, "r+b") as f:                                           # ... and its disk damages it
            f.seek(1000)
            f.write(b"\x00" * 100)
        bob.db.close()
        bob.start()
        bob.peer.table.add("Alice", "127.0.0.1", alice.tcp_port)
        self.assertTrue(wait_until(lambda: bob.states(fid)[-1:] == ["verified"], 40), bob.states(fid))
        self.assertTrue(any("partial file differs" in line for line in alice.lines))
        self.assert_identical(saved, path)

    def test_damaged_chunk_is_requested_again(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        self.introduce(alice, bob)
        fid, path, saved = self.start_transfer(alice, bob, delay=0.002)
        conn = alice.peer.connections["Bob"]
        real_send, damaged = conn.send, []

        def flaky_send(frame_type, fields=None, data=b""):
            if frame_type == "FILE_CHUNK" and fields["seq"] == 5 and not damaged:
                damaged.append(fields["seq"])
                data = b"\xff" + data[1:]                                       # one bit pattern changes on the way
            return real_send(frame_type, fields, data)
        conn.send = flaky_send
        self.assertTrue(wait_until(lambda: bob.states(fid)[-1:] == ["verified"], 30), bob.states(fid))
        self.assertEqual(damaged, [5])
        self.assertTrue(any("chunk 5 is damaged" in line for line in bob.lines))
        self.assertNotIn("interrupted", bob.states(fid))
        self.assert_identical(saved, path)

    def test_a_chunk_damaged_again_and_again_fails_the_transfer(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        self.introduce(alice, bob)
        fid, path, saved = self.start_transfer(alice, bob, delay=0.002)
        conn = alice.peer.connections["Bob"]
        real_send = conn.send
        conn.send = lambda t, f=None, d=b"": real_send(t, f, (b"\xff" + d[1:]) if t == "FILE_CHUNK" and f["seq"] == 3 else d)
        self.assertTrue(wait_until(lambda: bob.states(fid)[-1:] == ["failed"], 30), bob.states(fid))
        self.assertIn("damaged %d times" % (config.MAX_CHUNK_RETRIES + 1), bob.reasons(fid)[-1])
        self.assertFalse(os.path.exists(saved))

    def test_sender_restart_resumes_from_the_saved_state(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        self.introduce(alice, bob)
        fid, path, saved = self.start_transfer(alice, bob)
        self.assertTrue(wait_until(lambda: bob.done(fid) > 1024 * 1024))
        self.restart(alice)                                                     # the SENDER closes and restarts
        self.assertEqual(alice.peer.files.transfers[fid].state, "interrupted")   # loaded from the database
        alice.peer.table.add("Bob", "127.0.0.1", bob.tcp_port)
        alice.peer.ensure_connection("Bob")                                     # Bob is reachable again
        self.assertTrue(wait_until(lambda: bob.states(fid)[-1:] == ["verified"], 40), bob.states(fid))
        self.assertTrue(wait_until(lambda: alice.states(fid)[-1:] == ["verified"]), alice.states(fid))
        self.assert_identical(saved, path)

    def test_cancel_while_interrupted_removes_the_partial_file_on_both_sides(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        self.introduce(alice, bob)
        fid, path, saved = self.start_transfer(alice, bob, delay=0.02)
        self.assertTrue(wait_until(lambda: bob.done(fid) > 0))
        config.RESUME_DELAYS = (30,)                                            # keep it interrupted for now
        alice.peer.connections["Bob"].close()
        self.assertTrue(wait_until(lambda: bob.states(fid)[-1:] == ["interrupted"]), bob.states(fid))
        bob.peer.files.cancel(fid)
        self.assertFalse(os.path.exists(saved))
        self.assertTrue(wait_until(lambda: alice.states(fid)[-1:] == ["cancelled"]), alice.states(fid))
        self.assertEqual(bob.db.saved_transfers(), [])


class ReconnectTests(StageTwoBase):
    def test_a_dropped_connection_is_rebuilt_automatically(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        self.introduce(alice, bob)
        self.assertTrue(alice.peer.send_message("Bob", "hello"))
        first = alice.peer.connections["Bob"]
        self.assertTrue(wait_until(lambda: alice.db.outbox_count("Bob") == 0))  # ACKed: nothing to re-send
        with bob.peer.conn_lock:                                                # Bob's side breaks the link
            incoming = list(bob.peer.incoming)
        for conn in incoming:
            conn.close()
        self.assertTrue(wait_until(lambda: first.closed))
        self.assertTrue(wait_until(lambda: alice.peer.connections.get("Bob") not in (None, first)
                                   and not alice.peer.connections["Bob"].closed, 10), "no reconnection")
        self.assertTrue(wait_until(lambda: any("Reconnected to Bob" in line for line in alice.lines), 5))
        self.assertTrue(alice.peer.send_message("Bob", "still here"))
        self.assertEqual([bob.inbox.get(timeout=3)[1] for _ in range(2)], ["hello", "still here"])

    def test_reconnection_gives_up_after_a_bounded_number_of_attempts(self):
        config.RECONNECT_DELAYS = (0.05, 0.05, 0.05)
        alice, bob = self.node("Alice"), self.node("Bob")
        self.introduce(alice, bob)
        self.assertTrue(alice.peer.send_message("Bob", "hello"))
        bob.peer.stop()                                                         # Bob is gone for good
        self.assertTrue(wait_until(lambda: any("Gave up reconnecting to Bob" in l for l in alice.lines), 15))
        attempts = [l for l in alice.lines if "Reconnecting to Bob, attempt" in l]
        self.assertEqual(len(attempts), len(config.RECONNECT_DELAYS))
        self.assertTrue(wait_until(lambda: not alice.peer.reconnecting))
        self.nodes.remove(bob)
        bob.db.close()

    def test_no_reconnection_to_a_peer_that_left(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        self.introduce(alice, bob)
        self.assertTrue(alice.peer.send_message("Bob", "hello"))
        alice.peer.table.remove("Bob")                                          # as after a BYE
        alice.peer.connections["Bob"].close()
        time.sleep(1.5)
        self.assertFalse(any("Reconnecting to Bob" in l for l in alice.lines))
        self.assertNotIn("Bob", alice.peer.connections)


class ManualConnectTests(StageTwoBase):
    def test_connect_by_ip_without_discovery_and_talk_both_ways(self):
        alice, bob = self.node("Alice"), self.node("Bob")                       # different UDP ports: no discovery
        self.assertEqual(alice.peer.peer_names(), [])
        self.assertEqual(alice.peer.connect_to("127.0.0.1", bob.tcp_port), "Bob")
        self.assertIn("Bob", alice.peer.peer_names())
        self.assertTrue(alice.peer.send_message("Bob", "found you by IP"))
        self.assertEqual(bob.inbox.get(timeout=3), ("Alice", "found you by IP"))
        # Bob never discovered Alice either, but learned her server port from the handshake:
        self.assertTrue(wait_until(lambda: "Alice" in bob.peer.peer_names()))
        self.assertTrue(bob.peer.send_message("Alice", "and back"))
        self.assertEqual(alice.inbox.get(timeout=3), ("Bob", "and back"))

    def test_connect_async_reports_success_and_failure(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        statuses = queue.Queue()
        alice.peer.on_status = statuses.put
        alice.peer.connect_async("127.0.0.1:%d" % bob.tcp_port)
        self.assertIn("Connected to Bob", statuses.get(timeout=10))
        alice.peer.connect_async("127.0.0.1:%d" % free_port())                  # nobody listens there
        self.assertIn("Could not connect", statuses.get(timeout=10))
        alice.peer.connect_async("not an address!")
        self.assertIn("Cannot connect", statuses.get(timeout=10))

    def test_connecting_to_myself_is_refused(self):
        alice = self.node("Alice")
        with self.assertRaises(OSError):
            alice.peer.connect_to("127.0.0.1", alice.tcp_port)

    def test_a_peer_added_by_ip_stays_while_it_answers_pings(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        alice.peer.connect_to("127.0.0.1", bob.tcp_port)
        alice.peer.table.peers["Bob"]["last_seen"] -= config.PEER_TIMEOUT - 1  # nearly timed out (no HELLOs)
        alice.peer._ping_and_retry()                                            # PING -> PONG refreshes it
        self.assertTrue(wait_until(lambda: time.time() - alice.peer.table.peers["Bob"]["last_seen"] < 5))

    def test_parse_address(self):
        self.assertEqual(Peer.parse_address("10.0.0.7"), ("10.0.0.7", config.DEFAULT_TCP_PORT))
        self.assertEqual(Peer.parse_address(" 10.0.0.7:5002 "), ("10.0.0.7", 5002))
        for bad in ("", "10.0.0.7:0", "10.0.0.7:99999", "a b:5000", ":5000", "10.0.0.7:x"):
            with self.assertRaises(ValueError, msg=bad):
                Peer.parse_address(bad)


if __name__ == "__main__":
    unittest.main()
