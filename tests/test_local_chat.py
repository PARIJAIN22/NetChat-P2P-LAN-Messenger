"""
Starts two real peers on localhost (different TCP ports, same UDP port) and
checks that discovery and messaging work over real sockets.
"""

import os
import queue
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netchat.logger import EventLog
from netchat.peer import Peer

TEST_UDP_PORT = 50501      # not the default 5001, so tests never clash with a running app


def free_tcp_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))        # port 0 = "OS, give me any free port"
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until(condition, seconds=6):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.1)
    return False


class LocalChatTests(unittest.TestCase):
    def setUp(self):
        self.inbox_a = queue.Queue()
        self.inbox_b = queue.Queue()
        self.alice = Peer("Alice", free_tcp_port(), TEST_UDP_PORT, EventLog("Alice"),
                          on_message=lambda s, t, ts: self.inbox_a.put((s, t)))
        self.bob = Peer("Bob", free_tcp_port(), TEST_UDP_PORT, EventLog("Bob"),
                        on_message=lambda s, t, ts: self.inbox_b.put((s, t)))
        self.alice.start()
        self.bob.start()

    def tearDown(self):
        self.alice.stop()
        self.bob.stop()

    def introduce(self):
        """Put each peer in the other's table by hand, so TCP tests do not depend on UDP."""
        self.alice.table.add("Bob", "127.0.0.1", self.bob.tcp_port)
        self.bob.table.add("Alice", "127.0.0.1", self.alice.tcp_port)

    def test_message_arrives_over_tcp(self):
        self.introduce()
        text = "hello | Bob\nsecond line \\ end"      # includes the awkward characters
        self.assertTrue(self.alice.send_message("Bob", text))
        sender, got = self.inbox_b.get(timeout=3)
        self.assertEqual(sender, "Alice")
        self.assertEqual(got, text)

    def test_connection_is_reused_then_reconnects(self):
        self.introduce()
        self.assertTrue(self.alice.send_message("Bob", "one"))
        first_socket = self.alice.connections["Bob"]
        self.assertTrue(self.alice.send_message("Bob", "two"))
        self.assertIs(self.alice.connections["Bob"], first_socket)     # reused
        got = [self.inbox_b.get(timeout=3)[1] for _ in range(2)]
        # Break the connection behind Alice's back. We wait until Bob has the first two messages
        # AND Alice has seen their ACKs: a connection that is reset while data is still in flight
        # may lose that data (that case is covered by test_offline_queue.py, where the ACK +
        # re-send logic recovers it).
        self.assertTrue(wait_until(lambda: self.alice.db.outbox_count("Bob") == 0), "ACKs did not arrive")
        first_socket.close()
        self.assertTrue(self.alice.send_message("Bob", "three"))
        self.assertIsNot(self.alice.connections["Bob"], first_socket)  # reconnected
        got.append(self.inbox_b.get(timeout=3)[1])
        self.assertEqual(got, ["one", "two", "three"])

    def test_send_to_unknown_peer_fails_cleanly(self):
        self.assertFalse(self.alice.send_message("Nobody", "hi"))

    def test_unreachable_peer_is_removed(self):
        self.alice.table.add("Ghost", "127.0.0.1", free_tcp_port())    # nobody listens there
        self.assertFalse(self.alice.send_message("Ghost", "hi"))
        self.assertNotIn("Ghost", self.alice.peer_names())

    def test_discovery_and_bye(self):
        # real UDP broadcast/loopback discovery, no manual introduction
        self.assertTrue(wait_until(lambda: "Bob" in self.alice.peer_names()
                                   and "Alice" in self.bob.peer_names()),
                        "peers did not discover each other")
        self.assertTrue(self.alice.send_message("Bob", "via discovery"))
        self.assertEqual(self.inbox_b.get(timeout=3), ("Alice", "via discovery"))
        self.bob.stop()                                   # sends BYE
        self.assertTrue(wait_until(lambda: "Bob" not in self.alice.peer_names()),
                        "BYE did not remove the peer")


if __name__ == "__main__":
    unittest.main()
