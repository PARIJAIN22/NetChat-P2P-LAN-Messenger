"""
Settings, command-line/settings priority, network info maths, RTT measurement (PING/PONG),
changing the passphrase while running, restart arguments and the emoji list.
"""

import json
import os
import queue
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from netchat import config, crypto, netinfo, settings
from netchat.gui import restart_arguments
from netchat.gui_dialogs import EMOJIS
from netchat.logger import EventLog
from netchat.peer import Peer
from netchat.storage import Database

TEST_UDP_PORT = 50505


def free_tcp_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until(condition, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.05)
    return False


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "settings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_gives_defaults(self):
        self.assertEqual(settings.load(self.path), settings.DEFAULTS)

    def test_round_trip(self):
        new = dict(settings.DEFAULTS, username="Asha", tcp_port=6000, sound=False, dark_mode=True, passphrase="pw",
                   download_dir="C:\\Files")
        settings.save(self.path, new)
        self.assertEqual(settings.load(self.path), new)
        self.assertFalse(os.path.exists(self.path + ".tmp"))        # the temporary file was renamed away

    def test_damaged_files_never_crash(self):
        for content in ("", "{not json", "[1,2,3]", "null", '"text"'):
            with open(self.path, "w") as f:
                f.write(content)
            self.assertEqual(settings.load(self.path), settings.DEFAULTS, content)

    def test_values_of_the_wrong_type_fall_back_to_the_default(self):
        with open(self.path, "w") as f:
            json.dump({"username": 5, "tcp_port": "abc", "udp_port": True, "sound": "yes", "dark_mode": 1,
                       "unknown_key": 1}, f)
        self.assertEqual(settings.load(self.path), settings.DEFAULTS)

    def test_valid_port(self):
        self.assertEqual(settings.valid_port("5000"), 5000)
        self.assertEqual(settings.valid_port(" 65535 "), 65535)
        for bad in ("", "abc", "80", "1023", "65536", "-5", "5000.5"):
            self.assertIsNone(settings.valid_port(bad), bad)

    def test_command_line_beats_settings_beats_defaults(self):
        settings.save(self.path, dict(settings.DEFAULTS, username="FromFile", tcp_port=6001, udp_port=6002,
                                      passphrase="filepass"))
        args = main.parse_args(["--data-dir", self.tmp.name])
        self.assertEqual((args.name, args.tcp_port, args.udp_port, args.passphrase), ("FromFile", 6001, 6002, "filepass"))
        args = main.parse_args(["--data-dir", self.tmp.name, "--name", "FromCli", "--tcp-port", "7000"])
        self.assertEqual((args.name, args.tcp_port, args.udp_port), ("FromCli", 7000, 6002))
        args = main.parse_args(["--data-dir", os.path.join(self.tmp.name, "empty")])
        self.assertEqual((args.name, args.tcp_port, args.udp_port, args.passphrase),
                         (None, config.DEFAULT_TCP_PORT, config.DEFAULT_UDP_PORT, None))


class NetInfoTests(unittest.TestCase):
    def test_slash_24(self):
        info = netinfo.subnet_details("192.168.1.37", 24)
        self.assertEqual((info["netmask"], info["network"], info["directed_broadcast"]),
                         ("255.255.255.0", "192.168.1.0", "192.168.1.255"))
        self.assertEqual(info["discovery_broadcast"], "255.255.255.255")

    def test_other_prefixes(self):
        info = netinfo.subnet_details("172.17.84.61", 20)
        self.assertEqual((info["netmask"], info["network"], info["directed_broadcast"]),
                         ("255.255.240.0", "172.17.80.0", "172.17.95.255"))
        info = netinfo.subnet_details("10.1.2.3", 8)
        self.assertEqual((info["netmask"], info["directed_broadcast"]), ("255.0.0.0", "10.255.255.255"))
        info = netinfo.subnet_details("192.168.43.5", 28)                      # a phone hotspot style mask
        self.assertEqual((info["network"], info["directed_broadcast"]), ("192.168.43.0", "192.168.43.15"))

    def test_garbage_ip_is_assumed_24_and_never_runs_a_command(self):
        self.assertEqual(netinfo.detect_prefix_length("1.2.3.4; Remove-Item *"), (24, "assumed"))
        self.assertEqual(netinfo.detect_prefix_length("127.0.0.1"), (8, "adapter"))


class PeerFeatureTests(unittest.TestCase):
    def setUp(self):
        self.old_interval = config.PING_INTERVAL
        self.nodes = []

    def tearDown(self):
        config.PING_INTERVAL = self.old_interval
        for peer, _, _ in self.nodes:
            peer.stop()

    def node(self, name, **kwargs):
        inbox, events = queue.Queue(), []
        peer = Peer(name, free_tcp_port(), TEST_UDP_PORT, EventLog(name), on_message=lambda s, t, ts: inbox.put((s, t)),
                    on_event=lambda k, i: events.append((k, i)), db=Database(":memory:", name), **kwargs)
        peer.start()
        self.nodes.append((peer, inbox, events))
        return peer, inbox, events

    def test_rtt_is_measured_with_ping_and_pong(self):
        config.PING_INTERVAL = 1                                               # instead of 15 s, to keep the test short
        alice, _, a_events = self.node("Alice")
        bob, _, b_events = self.node("Bob")
        self.assertTrue(wait_until(lambda: "Bob" in alice.peer_names() and "Alice" in bob.peer_names()))
        self.assertTrue(wait_until(lambda: any(k == "rtt" for k, _ in a_events) and any(k == "rtt" for k, _ in b_events),
                                   12), "no PONG received")
        stats = {s["name"]: s for s in alice.peer_stats()}
        self.assertIn("Bob", stats)
        self.assertIsNotNone(stats["Bob"]["rtt"])
        # localhost: about a millisecond. A value like 40 ms would mean the connection set-up
        # (TCP + key exchange) was counted too; that was a real bug once.
        self.assertTrue(0 <= stats["Bob"]["rtt"] < 0.02, stats["Bob"]["rtt"])
        self.assertLess(time.time() - stats["Bob"]["last_seen"], 15)
        self.assertEqual(stats["Bob"]["port"], bob.tcp_port)
        self.assertEqual(stats["Bob"]["fingerprint"], bob.fingerprint if crypto.AVAILABLE else "")

    @unittest.skipUnless(crypto.AVAILABLE, "needs the cryptography package")
    def test_changing_the_passphrase_while_running(self):
        alice, _, a_events = self.node("Alice")
        bob, bob_inbox, _ = self.node("Bob")
        alice.table.add("Bob", "127.0.0.1", bob.tcp_port)
        bob.table.add("Alice", "127.0.0.1", alice.tcp_port)
        self.assertTrue(alice.send_message("Bob", "before"))
        bob_inbox.get(timeout=3)
        self.assertTrue(alice.connections)
        self.assertTrue(wait_until(lambda: alice.db.outbox_count() == 0), "ACK did not arrive")
        alice.set_passphrase("new secret")
        bob.set_passphrase("new secret")
        self.assertFalse(alice.connections, "old connections must be closed")
        self.assertTrue(alice.send_message("Bob", "after"))                    # a fresh connection with the new key
        self.assertEqual(bob_inbox.get(timeout=3), ("Alice", "after"))
        bob.set_passphrase("something else")                                   # now they disagree
        self.assertTrue(wait_until(lambda: not alice.connections), "Alice never noticed the closed connection")
        self.assertEqual(alice.deliver("Bob", "id-x", {"text": "no", "ts": "1"}), "failed")


class SmallThingsTests(unittest.TestCase):
    def test_restart_arguments_drop_what_settings_just_changed(self):
        argv = ["--name", "Asha", "--tcp-port", "5002", "--data-dir", "asha", "--passphrase=pw", "--log-file", "x.log",
                "--udp-port=5001", "--no-encryption"]
        self.assertEqual(restart_arguments(argv), ["--data-dir", "asha", "--log-file", "x.log", "--no-encryption"])
        self.assertEqual(restart_arguments([]), [])

    def test_there_are_40_different_emojis(self):
        self.assertEqual(len(EMOJIS), 40)
        self.assertEqual(len(set(EMOJIS)), 40)
        for emoji in EMOJIS:                                                   # they must survive the wire format
            from netchat import protocol
            frame = protocol.read_frame(__import__("io").BytesIO(protocol.encode_frame("MSG", {"id": "1", "text": emoji})).read)
            self.assertEqual(frame.get("text"), emoji)


if __name__ == "__main__":
    unittest.main()
