"""
Tests for the security and robustness fixes: duplicate names in discovery, authenticated BYE,
trust-on-first-use identity keys, ordered background delivery, ACK bookkeeping, file-transfer
races, a GUI event loop that survives errors, and a bounded stop().
"""

import hashlib
import os
import queue
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # for the shared helpers in test_crypto

from netchat import config, crypto, protocol
from netchat.discovery import Discovery, PeerTable
from netchat.logger import EventLog
from netchat.peer import Peer

import test_crypto
from test_crypto import free_tcp_port, free_udp_port, wait_until


def make_discovery(verifier=None, signer=None, name="Me", my_ip="10.0.0.1"):
    events = {"changes": 0, "conflicts": [], "lines": []}
    table = PeerTable()

    def changed():
        events["changes"] += 1
    d = Discovery(name, my_ip, 6000, 6001, table, events["lines"].append, changed, lambda text: None,
                  signer=signer, verifier=verifier,
                  on_conflict=lambda who, address: events["conflicts"].append((who, address)))
    return d, table, events


def datagram(kind, sender, payload):
    return protocol.encode(kind, sender, payload).encode("utf-8")


class DuplicateNameTests(unittest.TestCase):
    def test_second_address_for_a_live_name_is_ignored_and_reported(self):
        d, table, events = make_discovery()
        d._handle_datagram(datagram("REPLY", "Bob", "10.0.0.5:5000"), ("10.0.0.5", 6001))
        d._handle_datagram(datagram("REPLY", "Bob", "10.0.0.9:5000"), ("10.0.0.9", 6001))   # impostor
        self.assertEqual(table.get("Bob"), ("10.0.0.5", 5000))
        self.assertEqual(events["conflicts"], [("Bob", "10.0.0.9:5000")])
        d._handle_datagram(datagram("REPLY", "Bob", "10.0.0.9:5000"), ("10.0.0.9", 6001))
        self.assertEqual(len(events["conflicts"]), 1)                                       # reported once

    def test_the_other_address_takes_over_once_the_first_goes_quiet(self):
        table = PeerTable()
        self.assertEqual(table.claim("Bob", "10.0.0.5", 5000), "new")
        self.assertEqual(table.claim("Bob", "10.0.0.5", 5000), "same")
        table.peers["Bob"]["last_seen"] -= config.PEER_TIMEOUT + 1
        self.assertEqual(table.claim("Bob", "10.0.0.9", 5000), "new")
        self.assertEqual(table.get("Bob"), ("10.0.0.9", 5000))

    def test_loopback_and_lan_ip_of_this_pc_are_the_same_peer(self):
        d, table, _ = make_discovery(my_ip="10.0.0.1")
        table.add("Bob", "127.0.0.1", 5000)
        d._handle_datagram(datagram("REPLY", "Bob", "10.0.0.1:5000"), ("10.0.0.1", 6001))
        self.assertEqual(table.get("Bob"), ("127.0.0.1", 5000))


class UnsignedByeTests(unittest.TestCase):
    def test_bye_from_another_address_is_ignored(self):
        d, table, events = make_discovery()
        table.add("Bob", "10.0.0.5", 5000)
        d._handle_datagram(datagram("BYE", "Bob", "10.0.0.5:5000"), ("10.0.0.66", 6001))   # spoofed
        self.assertIn("Bob", table.names())
        d._handle_datagram(datagram("BYE", "Bob", "10.0.0.5:5000"), ("10.0.0.5", 6001))    # the real one
        self.assertNotIn("Bob", table.names())
        self.assertEqual(events["changes"], 1)

    def test_a_pinned_peer_needs_a_signature(self):
        d, table, _ = make_discovery(verifier=lambda name, text, sig: False if name == "Bob" else None)
        table.add("Bob", "10.0.0.5", 5000)
        d._handle_datagram(datagram("BYE", "Bob", "10.0.0.5:5000"), ("10.0.0.5", 6001))
        self.assertIn("Bob", table.names())


@unittest.skipUnless(crypto.AVAILABLE, "the optional 'cryptography' package is not installed")
class SignedByeTests(unittest.TestCase):
    def setUp(self):
        self.bob_key = crypto.Identity()
        pinned = {"Bob": self.bob_key.public}

        def verifier(name, text, signature):
            return None if name not in pinned else crypto.verify(pinned[name], text, signature)
        self.bob, _, _ = make_discovery(signer=self.bob_key.sign, name="Bob", my_ip="10.0.0.5")
        self.bob.tcp_port = 5000
        self.alice, self.table, _ = make_discovery(verifier=verifier)
        self.table.add("Bob", "10.0.0.5", 5000)

    def test_valid_signature_removes_the_peer_even_from_another_address(self):
        self.alice._handle_datagram(datagram("BYE", "Bob", self.bob._bye_payload()), ("10.0.0.77", 6001))
        self.assertNotIn("Bob", self.table.names())

    def test_forged_and_replayed_byes_are_ignored(self):
        payload = self.bob._bye_payload()
        address, when, sig = payload.split(";")
        forged = ";".join([address, "t=%d" % (int(when[2:]) + 1), sig])            # signature no longer fits
        self.alice._handle_datagram(datagram("BYE", "Bob", forged), ("10.0.0.5", 6001))
        self.assertIn("Bob", self.table.names())
        other = crypto.Identity()                                                    # signed by a wrong key
        self.alice._handle_datagram(datagram("BYE", "Bob", "%s;%s;sig=%s" % (
            address, when, other.sign(Discovery._bye_text("Bob", address, when[2:])).hex())), ("10.0.0.5", 6001))
        self.assertIn("Bob", self.table.names())

        self.alice._handle_datagram(datagram("BYE", "Bob", payload), ("10.0.0.5", 6001))
        self.assertNotIn("Bob", self.table.names())
        self.table.add("Bob", "10.0.0.5", 5000)                                      # Bob comes back ...
        self.alice._handle_datagram(datagram("BYE", "Bob", payload), ("10.0.0.5", 6001))   # ... old BYE replayed
        self.assertIn("Bob", self.table.names())


class IdentityPinningTests(test_crypto.EncryptedPeerTests):
    """Reuses the Node / link helpers of the encryption tests. Only the tests below run here."""

    def assert_fails_with_identity(self, sender, recipient):
        msg_id = "m%d" % time.time_ns()
        result = sender.peer.deliver(recipient, msg_id, {"text": "secret", "ts": "12:00:00"})
        self.assertEqual(result, "failed")
        reasons = [i.get("reason", "") for k, i in sender.events
                   if k == "msg_status" and i["id"] == msg_id and i["status"] == "failed"]
        self.assertTrue(reasons and reasons[0], "no failure reason reported")

    def introduce_impostor(self, alice, impostor):
        alice.peer._drop_connection("Bob")
        alice.peer.table.add("Bob", "127.0.0.1", impostor.peer.tcp_port)
        impostor.peer.table.add("Alice", "127.0.0.1", alice.peer.tcp_port)

    def test_impostor_with_another_key_is_refused_in_both_directions(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        self.link(alice, bob)
        self.assertTrue(alice.peer.send_message("Bob", "hi"))
        self.assertEqual(bob.inbox.get(timeout=3), ("Alice", "hi"))
        self.assertEqual(alice.db.peer_key("Bob"), bob.peer.identity.public.hex())      # pinned on first use

        impostor = self.node("Bob")                                                    # new key, same name
        self.introduce_impostor(alice, impostor)
        self.assert_fails_with_identity(alice, "Bob")
        self.assertTrue(any(k == "identity" for k, _ in alice.events))
        self.assertEqual(alice.db.peer_key("Bob"), bob.peer.identity.public.hex())      # pin unchanged

        self.assertFalse(impostor.peer.send_message("Alice", "I am Bob, really"))      # dialing Alice
        time.sleep(0.3)
        self.assertTrue(alice.inbox.empty())

        # The user compares fingerprints and accepts the new key: now it works.
        alice.peer.trust_identity("Bob", impostor.peer.identity.public.hex())
        self.assertTrue(alice.peer.send_message("Bob", "welcome back"))
        self.assertEqual(impostor.inbox.get(timeout=3), ("Alice", "welcome back"))

    def test_unencrypted_impostor_cannot_skip_the_key_exchange(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        self.link(alice, bob)
        self.assertTrue(alice.peer.send_message("Bob", "hi"))
        self.assertEqual(bob.inbox.get(timeout=3), ("Alice", "hi"))
        impostor = self.node("Bob", encrypt=False)
        self.introduce_impostor(alice, impostor)
        self.assert_fails_with_identity(alice, "Bob")
        self.assertFalse(impostor.peer.send_message("Alice", "downgrade"))
        time.sleep(0.3)
        self.assertTrue(alice.inbox.empty())


# Only the new tests should run in IdentityPinningTests, not the inherited encryption tests again.
for _name in [n for n in dir(test_crypto.EncryptedPeerTests) if n.startswith("test_")]:
    setattr(IdentityPinningTests, _name, None)
IdentityPinningTests = unittest.skipUnless(crypto.AVAILABLE, "the optional 'cryptography' package is not installed")(
    IdentityPinningTests)


class FakeConn:
    def __init__(self, name, address=("127.0.0.1", 1)):
        self.remote_name, self.address, self.sent, self.closed = name, address, [], False

    def send(self, frame_type, fields=None, data=b""):
        self.sent.append((frame_type, fields))


class PeerLogicTests(unittest.TestCase):
    def setUp(self):
        self.lines = []
        log = EventLog("T")
        log.add_listener(self.lines.append)
        self.received = []
        self.peer = Peer("Alice", free_tcp_port(), free_udp_port(), log, encrypt=False,
                         on_message=lambda s, t, ts: self.received.append((s, t)))

    def tearDown(self):
        self.peer.stop()

    def test_unencrypted_connection_must_come_from_the_discovered_address(self):
        self.peer.table.add("Bob", "10.0.0.5", 5000)
        with self.assertRaises(OSError):
            self.peer._check_address(FakeConn("Bob", ("10.0.0.66", 4000)))
        self.peer._check_address(FakeConn("Bob", ("10.0.0.5", 4000)))
        self.peer._check_address(FakeConn("Unknown", ("10.0.0.66", 4000)))     # nothing to compare with

    def test_same_message_id_from_two_senders_is_not_a_duplicate(self):
        for name in ("Bob", "Eve", "Bob"):
            self.peer._handle_msg(FakeConn(name), protocol.Frame("MSG", {"id": "same", "text": "hi " + name}))
        self.assertEqual(self.received, [("Bob", "hi Bob"), ("Eve", "hi Eve")])

    def test_ack_is_expected_before_the_frame_leaves(self):
        self.peer.table.add("Bob", "127.0.0.1", 1)
        seen = []
        self.peer._send_frame = lambda name, kind, fields: seen.append((fields["id"], name) in self.peer.pending_acks)
        self.assertEqual(self.peer._send_queued("Bob", "m1", {"text": "x"}), "sent")
        self.assertEqual(seen, [True])

        def broken(*args):
            raise OSError("down")
        self.peer._send_frame = broken
        self.assertEqual(self.peer._send_queued("Bob", "m2", {"text": "x"}), "queued")
        self.assertNotIn(("m2", "Bob"), self.peer.pending_acks)

    def test_deliver_async_keeps_the_order_per_recipient(self):
        order = []

        def slow_deliver(recipient, msg_id, fields):
            time.sleep(0.01 if int(msg_id) % 2 else 0)
            order.append((recipient, msg_id))
        self.peer.deliver = slow_deliver
        for i in range(30):
            self.peer.deliver_async("Bob" if i % 3 else "Eve", str(i), {"text": str(i)})
        self.assertTrue(wait_until(lambda: len(order) == 30))
        for who in ("Bob", "Eve"):
            ids = [int(m) for r, m in order if r == who]
            self.assertEqual(ids, sorted(ids))
        self.assertTrue(wait_until(lambda: not self.peer.send_queues))                # workers finished

    def test_crashing_delivery_is_reported_not_silent(self):
        events = []
        self.peer.on_event = lambda kind, info: events.append((kind, info))

        def crash(*args):
            raise RuntimeError("database is locked")
        self.peer.deliver = crash
        self.peer.deliver_async("Bob", "m1", {"text": "x"})
        self.assertTrue(wait_until(lambda: any(i.get("status") == "failed" for _, i in events)))
        self.assertTrue(any("crashed" in line for line in self.lines))

    def test_stop_waits_a_bounded_time_for_threads(self):
        self.peer.start()
        self.peer._spawn(time.sleep, 30)                       # a thread that will not finish soon
        started = time.time()
        self.peer.stop()
        self.assertLess(time.time() - started, config.STOP_TIMEOUT + 1.5)


class FileRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.peer = Peer("Alice", free_tcp_port(), free_udp_port(), EventLog("T"), encrypt=False,
                         download_dir=self.tmp.name)
        self.conn = FakeConn("Bob")
        self.data = b"x" * 1000
        self.peer.files._on_file_offer(self.conn, protocol.Frame("FILE_OFFER", {
            "fid": "f1", "name": "a.bin", "size": len(self.data), "sha256": hashlib.sha256(self.data).hexdigest()}))
        self.transfer = self.peer.files.transfers["f1"]

    def tearDown(self):
        self.peer.stop()
        self.tmp.cleanup()

    def test_chunks_and_done_after_cancel_are_harmless(self):
        path = self.peer.files.accept("f1")
        self.assertTrue(os.path.exists(path))
        self.peer.files.cancel("f1")
        self.assertFalse(os.path.exists(path))
        # Frames that were already on the way when the user clicked Cancel:
        self.peer.files._on_file_chunk(self.conn, protocol.Frame("FILE_CHUNK", {"fid": "f1", "seq": 0}, self.data))
        self.peer.files._on_file_done(self.conn, protocol.Frame("FILE_DONE", {"fid": "f1"}))
        self.assertEqual(self.transfer.state, "cancelled")
        self.assertFalse(os.path.exists(path))

    def test_cancel_racing_with_chunks_never_crashes(self):
        self.peer.files.accept("f1")
        errors = []

        def feed():
            try:
                for seq in range(200):
                    self.peer.files._on_file_chunk(self.conn, protocol.Frame("FILE_CHUNK", {"fid": "f1", "seq": seq}, b"y"))
            except Exception as e:                                # before the fix: write to a closed file
                errors.append(e)
        worker = threading.Thread(target=feed)
        worker.start()
        self.peer.files.cancel("f1")
        worker.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.transfer.state, "cancelled")
        self.assertIsNone(self.transfer.handle)

    def test_the_same_offer_twice_creates_one_transfer(self):
        first = self.peer.files.transfers["f1"]
        self.peer.files._on_file_offer(self.conn, protocol.Frame("FILE_OFFER", {
            "fid": "f1", "name": "b.bin", "size": 1, "sha256": "0" * 64}))
        self.assertIs(self.peer.files.transfers["f1"], first)


class GuiEventLoopTests(unittest.TestCase):
    """_poll_events without a window: a fake `self` with just what the method uses."""

    def fake_app(self, handler):
        from netchat.gui import ChatApp
        scheduled, lines = [], []
        app = types.SimpleNamespace(events=queue.Queue(), log=types.SimpleNamespace(log=lines.append),
                                    root=types.SimpleNamespace(after=lambda ms, f: scheduled.append(ms)),
                                    _handle_event=handler, _poll_events=None)
        return ChatApp, app, scheduled, lines

    def test_a_failing_handler_does_not_stop_the_loop(self):
        handled = []

        def handler(event):
            if event[0] == "bad":
                raise KeyError("boom")
            handled.append(event)
        ChatApp, app, scheduled, lines = self.fake_app(handler)
        for event in (("bad", 1), ("good", 2)):
            app.events.put(event)
        ChatApp._poll_events(app)
        self.assertEqual(handled, [("good", 2)])
        self.assertEqual(scheduled, [100])                   # the loop is scheduled again
        self.assertTrue(any("boom" in line for line in lines))

    def test_a_flood_of_events_is_handled_in_slices(self):
        ChatApp, app, scheduled, _ = self.fake_app(lambda event: time.sleep(0.001))
        for i in range(2000):
            app.events.put(("log", i))
        started = time.time()
        ChatApp._poll_events(app)
        self.assertLess(time.time() - started, 0.5)          # the window gets control back quickly
        self.assertFalse(app.events.empty())
        self.assertEqual(scheduled, [10])                    # and the rest follows right away


if __name__ == "__main__":
    unittest.main()
