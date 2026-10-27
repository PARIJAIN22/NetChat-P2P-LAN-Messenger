"""
Encryption tests (skipped automatically if the optional `cryptography` package is missing).

The most convincing ones put a small TCP proxy between two peers: it records every byte that
crosses the "network", exactly like someone with Wireshark would, and can also tamper with it.
"""

import os
import queue
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netchat import crypto, protocol
from netchat.connection import Connection
from netchat.logger import EventLog
from netchat.peer import Peer
from netchat.storage import Database

TEST_UDP_PORT = 50504


def free_tcp_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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


class Proxy:
    """Sits between a dialer and a listener, records everything, optionally flips one byte."""

    def __init__(self, target_port):
        self.target_port = target_port
        self.captured = bytearray()
        self.tamper_after = None            # flip one byte once this many bytes went client -> server
        self.sent_to_server = 0
        self.tampered = False
        self.sockets = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.listener.settimeout(0.3)
        self.port = self.listener.getsockname()[1]
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.running:
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            server = socket.create_connection(("127.0.0.1", self.target_port))
            self.sockets += [client, server]
            threading.Thread(target=self._pump, args=(client, server, True), daemon=True).start()
            threading.Thread(target=self._pump, args=(server, client, False), daemon=True).start()

    def _pump(self, source, target, client_to_server):
        try:
            while self.running:
                data = source.recv(65536)
                if not data:
                    break
                self.captured += data
                if client_to_server:
                    start = self.sent_to_server
                    self.sent_to_server += len(data)
                    if (self.tamper_after is not None and not self.tampered and self.sent_to_server > self.tamper_after):
                        index = max(0, self.tamper_after - start)
                        data = bytearray(data)
                        data[min(index, len(data) - 1)] ^= 0x01        # one single flipped bit
                        data = bytes(data)
                        self.tampered = True
                target.sendall(data)
        except OSError:
            pass
        finally:
            for s in (source, target):
                try:
                    s.close()
                except OSError:
                    pass

    def stop(self):
        self.running = False
        for s in self.sockets + [self.listener]:
            try:
                s.close()
            except OSError:
                pass


class Node:
    def __init__(self, name, **kwargs):
        self.inbox = queue.Queue()
        self.events = []
        self.lines = []                                   # this peer's log lines
        log = EventLog(name)
        log.add_listener(self.lines.append)
        self.db = Database(":memory:", name)
        # Every node gets its OWN UDP port, so discovery cannot find the other node and overwrite the
        # address (or the recording proxy) that the test put in the peer table.
        self.peer = Peer(name, free_tcp_port(), free_udp_port(), log,
                         on_message=lambda s, t, ts: self.inbox.put((s, t)),
                         on_event=lambda kind, info: self.events.append((kind, info)), db=self.db, **kwargs)

    def statuses(self, msg_id):
        return [i["status"] for k, i in self.events if k == "msg_status" and i["id"] == msg_id]

    def secure_events(self):
        return [i for k, i in self.events if k == "secure"]


@unittest.skipUnless(crypto.AVAILABLE, "the optional 'cryptography' package is not installed")
class CryptoPrimitiveTests(unittest.TestCase):
    def pair(self, dialer_pass="", listener_pass=""):
        d = crypto.Handshake(crypto.Identity(), dialer_pass, "dialer")
        l = crypto.Handshake(crypto.Identity(), listener_pass, "listener")
        reply = l.listener_accept(d.dialer_hello())
        return d, l, reply

    def test_both_sides_derive_the_same_keys_and_can_talk(self):
        d, l, reply = self.pair("secret", "secret")
        auth = d.dialer_finish(reply)
        l.listener_verify_auth(auth)
        a, b = d.channel(), l.channel()
        record = a.seal(b"hello from the dialer")
        self.assertEqual(b.open(record[4:]), b"hello from the dialer")
        self.assertEqual(a.open(b.seal(b"and back")[4:]), b"and back")
        self.assertNotIn(b"hello", record)                              # really encrypted

    def test_wrong_passphrase_is_detected_by_the_dialer(self):
        d, l, reply = self.pair("one", "two")
        with self.assertRaises(crypto.CryptoError):
            d.dialer_finish(reply)

    def test_passphrase_versus_none_is_detected(self):
        d, l, reply = self.pair("", "set")
        with self.assertRaises(crypto.CryptoError):
            d.dialer_finish(reply)

    def test_wrong_auth_proof_is_rejected_by_the_listener(self):
        d, l, reply = self.pair()
        d.dialer_finish(reply)
        with self.assertRaises(crypto.CryptoError):
            l.listener_verify_auth({"proof": "00" * 32})

    def test_tampered_record_fails(self):
        d, l, reply = self.pair()
        l.listener_verify_auth(d.dialer_finish(reply))
        a, b = d.channel(), l.channel()
        record = bytearray(a.seal(b"pay 10 euro"))
        record[10] ^= 0x01
        with self.assertRaises(crypto.CryptoError):
            b.open(bytes(record[4:]))

    def test_replayed_or_reordered_record_fails(self):
        d, l, reply = self.pair()
        l.listener_verify_auth(d.dialer_finish(reply))
        a, b = d.channel(), l.channel()
        first, second = a.seal(b"one")[4:], a.seal(b"two")[4:]
        self.assertEqual(b.open(first), b"one")
        with self.assertRaises(crypto.CryptoError):
            b.open(first)                                               # replay of the first record
        c = l.channel()
        with self.assertRaises(crypto.CryptoError):
            c.open(second)                                              # the second one arriving first

    def test_forged_signature_is_rejected(self):
        d = crypto.Handshake(crypto.Identity(), "", "dialer")
        l = crypto.Handshake(crypto.Identity(), "", "listener")
        hello = d.dialer_hello()
        hello["sig"] = "00" * 64
        with self.assertRaises(crypto.CryptoError):
            l.listener_accept(hello)
        for broken in ({}, {"ident": "zz", "eph": "00", "sig": "00"}):
            with self.assertRaises(crypto.CryptoError):
                l.listener_accept(broken)

    def test_fingerprint_is_stable_per_identity_and_different_between_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "identity.key")
            first, again = crypto.Identity(path), crypto.Identity(path)
            self.assertEqual(first.fingerprint, again.fingerprint)
            self.assertEqual(first.public, again.public)
            self.assertNotEqual(first.fingerprint, crypto.Identity().fingerprint)
            self.assertRegex(first.fingerprint, r"^([0-9A-F]{4} ){4}[0-9A-F]{4}$")
            with open(path, "wb") as f:                                 # a damaged key file is replaced
                f.write(b"garbage")
            self.assertEqual(len(crypto.Identity(path).public), 32)


@unittest.skipUnless(crypto.AVAILABLE, "the optional 'cryptography' package is not installed")
class EncryptedPeerTests(unittest.TestCase):
    def setUp(self):
        self.nodes, self.proxies = [], []

    def tearDown(self):
        for node in self.nodes:
            node.peer.stop()
        for proxy in self.proxies:
            proxy.stop()

    def node(self, name, **kwargs):
        node = Node(name, **kwargs)
        node.peer.start()
        self.nodes.append(node)
        return node

    def link(self, a, b, via_proxy=False):
        """Make a and b know each other; optionally route a -> b through a recording proxy."""
        port = b.peer.tcp_port
        if via_proxy:
            proxy = Proxy(b.peer.tcp_port)
            self.proxies.append(proxy)
            port = proxy.port
        a.peer.table.add(b.peer.name, "127.0.0.1", port)
        b.peer.table.add(a.peer.name, "127.0.0.1", a.peer.tcp_port)
        return proxy if via_proxy else None

    def test_connection_is_encrypted_and_fingerprints_match_the_identities(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        self.link(alice, bob)
        self.assertTrue(alice.peer.send_message("Bob", "hi"))
        self.assertEqual(bob.inbox.get(timeout=3), ("Alice", "hi"))
        self.assertTrue(wait_until(lambda: alice.secure_events() and bob.secure_events()))
        a_view, b_view = alice.secure_events()[0], bob.secure_events()[0]
        self.assertTrue(a_view["encrypted"] and b_view["encrypted"])
        self.assertEqual(a_view["fingerprint"], bob.peer.fingerprint)        # what Alice sees = Bob's real key
        self.assertEqual(b_view["fingerprint"], alice.peer.fingerprint)
        self.assertNotEqual(alice.peer.fingerprint, bob.peer.fingerprint)

    def test_message_text_is_invisible_on_the_wire(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        proxy = self.link(alice, bob, via_proxy=True)
        secret = "the launch code is 4815162342"
        self.assertTrue(alice.peer.send_message("Bob", secret))
        self.assertEqual(bob.inbox.get(timeout=3), ("Alice", secret))       # Bob can read it...
        self.assertGreater(len(proxy.captured), 100)
        self.assertNotIn(b"4815162342", bytes(proxy.captured))               # ...the wire cannot
        self.assertNotIn(b"launch", bytes(proxy.captured))

    def test_control_without_encryption_the_wire_shows_the_text(self):
        # the same experiment with encryption OFF proves the test above really tests something
        alice, bob = self.node("Alice", encrypt=False), self.node("Bob", encrypt=False)
        proxy = self.link(alice, bob, via_proxy=True)
        alice.peer.send_message("Bob", "the launch code is 4815162342")
        bob.inbox.get(timeout=3)
        self.assertIn(b"4815162342", bytes(proxy.captured))
        self.assertTrue(wait_until(lambda: alice.secure_events()))
        self.assertFalse(alice.secure_events()[0]["encrypted"])

    def test_a_changed_byte_on_the_wire_is_detected_and_the_message_is_sent_again(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        proxy = self.link(alice, bob, via_proxy=True)
        alice.peer.send_message("Bob", "warm up")                            # handshake + first record pass
        bob.inbox.get(timeout=3)
        proxy.tamper_after = proxy.sent_to_server + 20                       # flip a bit inside the NEXT record
        alice.peer.send_message("Bob", "tamper with me")
        got = bob.inbox.get(timeout=10)
        self.assertTrue(proxy.tampered)
        self.assertEqual(got, ("Alice", "tamper with me"))                   # never garbage: only the clean re-send
        self.assertTrue(any("authentication check" in line for line in bob.lines),
                        "Bob did not notice the damaged record")              # the drop is logged
        time.sleep(0.5)
        self.assertTrue(bob.inbox.empty())                                    # and exactly once

    def test_same_passphrase_works(self):
        alice, bob = self.node("Alice", passphrase="correct horse"), self.node("Bob", passphrase="correct horse")
        self.link(alice, bob)
        self.assertTrue(alice.peer.send_message("Bob", "members only"))
        self.assertEqual(bob.inbox.get(timeout=3), ("Alice", "members only"))

    def test_different_passphrase_is_rejected_with_a_clear_reason(self):
        alice, bob = self.node("Alice", passphrase="one"), self.node("Bob", passphrase="two")
        self.link(alice, bob)
        msg_id = uuid.uuid4().hex
        self.assertEqual(alice.peer.deliver("Bob", msg_id, {"text": "let me in", "ts": "10:00:00"}), "failed")
        self.assertIn("failed", alice.statuses(msg_id))
        failed = [i for k, i in alice.events if k == "msg_status" and i["id"] == msg_id and i["status"] == "failed"]
        self.assertIn("passphrase", failed[0]["reason"])
        time.sleep(0.3)
        self.assertTrue(bob.inbox.empty())
        self.assertEqual(alice.db.outbox_count(), 0)                         # not queued forever: it can never work

    def test_passphrase_versus_none_is_rejected_in_both_directions(self):
        alice, bob = self.node("Alice", passphrase="secret"), self.node("Bob")
        self.link(alice, bob)
        self.assertEqual(alice.peer.deliver("Bob", uuid.uuid4().hex, {"text": "x", "ts": "1"}), "failed")
        self.assertEqual(bob.peer.deliver("Alice", uuid.uuid4().hex, {"text": "y", "ts": "1"}), "failed")
        time.sleep(0.3)
        self.assertTrue(alice.inbox.empty() and bob.inbox.empty())

    def test_peer_that_cannot_encrypt_still_chats_when_no_passphrase_is_set(self):
        alice, bob = self.node("Alice"), self.node("Bob", encrypt=False)
        self.link(alice, bob)
        self.assertTrue(alice.peer.send_message("Bob", "plain but works"))
        self.assertEqual(bob.inbox.get(timeout=3), ("Alice", "plain but works"))
        self.assertTrue(wait_until(lambda: alice.secure_events()))
        self.assertFalse(alice.secure_events()[0]["encrypted"])              # the GUI shows an OPEN lock

    def test_passphrase_means_a_plaintext_client_is_refused(self):
        bob = self.node("Bob", passphrase="secret")
        sock = socket.create_connection(("127.0.0.1", bob.peer.tcp_port), timeout=3)
        conn = Connection(sock, ("127.0.0.1", 0), outgoing=True)
        conn.send("HI", {"name": "Mallory", "pass": True})                   # claims a passphrase but sends no keys
        frame = conn.read_frame(threading.Event(), time.time() + 3)
        self.assertEqual((frame.type, frame.get("code")), ("ERROR", "auth"))
        conn.close()

    def test_attacker_with_valid_keys_but_wrong_proof_is_dropped(self):
        bob = self.node("Bob")
        sock = socket.create_connection(("127.0.0.1", bob.peer.tcp_port), timeout=3)
        conn = Connection(sock, ("127.0.0.1", 0), outgoing=True)
        hs = crypto.Handshake(crypto.Identity(), "", "dialer")
        conn.send("HI", dict({"name": "Mallory", "pass": False}, **hs.dialer_hello()))
        reply = conn.read_frame(threading.Event(), time.time() + 3)
        self.assertEqual(reply.type, "HI")
        conn.send("AUTH", {"proof": "00" * 32})                              # cannot compute the real proof
        frame = conn.read_frame(threading.Event(), time.time() + 3)
        self.assertEqual((frame.type, frame.get("code")), ("ERROR", "auth"))
        conn.close()

    def test_files_are_encrypted_too(self):
        alice, bob = self.node("Alice"), self.node("Bob")
        proxy = self.link(alice, bob, via_proxy=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plain.txt")
            with open(path, "wb") as f:
                f.write(b"FILE-CONTENT-MARKER-XYZZY " * 4000)
            bob.peer.download_dir = os.path.join(tmp, "dl")
            offers = queue.Queue()
            bob.peer.on_event = lambda k, i: offers.put(i) if k == "file_offer" else None
            fid = alice.peer.files.offer("Bob", path)
            offer = offers.get(timeout=5)
            saved = bob.peer.files.accept(offer["fid"])
            self.assertTrue(wait_until(lambda: os.path.exists(saved) and os.path.getsize(saved) == 26 * 4000))
            time.sleep(0.3)
        self.assertNotIn(b"XYZZY", bytes(proxy.captured))


if __name__ == "__main__":
    unittest.main()
