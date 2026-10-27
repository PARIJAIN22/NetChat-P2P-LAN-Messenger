"""
discovery.py - finding other peers on the LAN using UDP.

Why UDP broadcast? We have no central server, so a new peer does not know
anybody's address. It shouts "HELLO, I am Rahul at 192.168.1.5:5000" to the
broadcast address; every NetChat on the LAN hears it and answers with a REPLY
sent directly to the newcomer. UDP is perfect here: no connection needed, and
only UDP can broadcast.

This file contains:
  * PeerTable  - the list of online peers (shared by threads, so it has a lock)
  * Discovery  - the UDP socket, the listener thread and the heartbeat thread
"""

import socket
import threading
import time

from . import config
from . import protocol


def get_local_ip():
    """Find the IP address this computer uses on the LAN."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # connect() on a UDP socket sends NO packet; it only makes the OS pick
        # the network interface it would use to reach that address.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return config.LOOPBACK_ADDRESS       # no network at all (e.g. Wi-Fi off)
    finally:
        s.close()


class PeerTable:
    """name -> (ip, tcp_port, last_seen). Several threads touch it, so every method locks."""

    def __init__(self):
        self.lock = threading.Lock()
        self.peers = {}

    def add(self, name, ip, port):
        """Add or refresh a peer. Returns True if this is new or its address changed."""
        with self.lock:
            old = self.peers.get(name)
            changed = old is None or (old["ip"], old["port"]) != (ip, port)
            self.peers[name] = {"ip": ip, "port": port, "last_seen": time.time()}
            return changed

    def claim(self, name, ip, port, same_host=None):
        """Add or refresh a peer heard via HELLO / REPLY, guarding against a second computer that
        uses the same name (by mistake or to impersonate). Returns
          'new'      the name was unknown (or its old address went quiet): stored
          'same'     the known peer refreshed itself
          'conflict' a DIFFERENT address claims a name whose current owner is still alive: ignored.
        First come, first served: the other address takes over only once the first one has
        been silent for PEER_TIMEOUT (or said BYE)."""
        same_host = same_host or (lambda a, b: a == b)
        now = time.time()
        with self.lock:
            old = self.peers.get(name)
            if old is not None and old["port"] == port and same_host(old["ip"], ip):
                old["last_seen"] = now
                return "same"
            if old is not None and now - old["last_seen"] <= config.PEER_TIMEOUT:
                return "conflict"
            self.peers[name] = {"ip": ip, "port": port, "last_seen": now}
            return "new"

    def touch(self, name):
        """A sign of life over TCP (PONG): keeps peers that UDP cannot reach (added by IP) in the table."""
        with self.lock:
            if name in self.peers:
                self.peers[name]["last_seen"] = time.time()

    def remove(self, name):
        """Remove a peer. Returns True if it was in the table."""
        with self.lock:
            return self.peers.pop(name, None) is not None

    def get(self, name):
        """Returns (ip, port) or None."""
        with self.lock:
            p = self.peers.get(name)
            if p is None:
                return None
            return p["ip"], p["port"]

    def names(self):
        with self.lock:
            return sorted(self.peers)

    def snapshot(self):
        """A copy of the whole table: {name: {'ip', 'port', 'last_seen'}} (for the Network info panel)."""
        with self.lock:
            return {name: dict(info) for name, info in self.peers.items()}

    def remove_stale(self, timeout):
        """Drop peers not heard from for `timeout` seconds. Returns their names."""
        now = time.time()
        with self.lock:
            stale = [n for n, p in self.peers.items() if now - p["last_seen"] > timeout]
            for n in stale:
                del self.peers[n]
            return stale


class Discovery:
    def __init__(self, name, my_ip, tcp_port, udp_port, table, log, on_change, on_status,
                 signer=None, verifier=None, on_conflict=None):
        self.name = name
        self.my_ip = my_ip
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.table = table
        self.log = log
        self.on_change = on_change      # called when the peer list changed
        self.on_status = on_status      # called with a text for the user
        self.sock = None
        self.stopping = threading.Event()
        self.threads = []
        self.broadcast_warned = False   # only warn the user once about a failing broadcast
        # BYE authentication (see _handle_bye). signer(bytes) -> signature bytes (None: no identity key).
        # verifier(name, bytes, signature) -> True / False, or None if we know no key for that name.
        self.signer = signer
        self.verifier = verifier
        self.on_conflict = on_conflict or (lambda name, address: None)
        self.conflicts_reported = set()  # (name, ip, port) already reported, so we warn only once
        self.last_bye = {}              # name -> newest BYE time accepted (replay protection)

    # ------------------------------------------------------------------
    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # SO_REUSEADDR: lets several NetChat instances on ONE computer bind the
        # same UDP port (handy for testing with 2-3 windows).
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_BROADCAST: the OS refuses to send to a broadcast address unless we ask for it.
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            self.sock.bind(("", self.udp_port))     # "" = listen on all network interfaces
        except OSError:
            self.sock.close()
            raise
        # Timeout so recvfrom() wakes up regularly and the thread can see "stopping".
        self.sock.settimeout(config.SOCKET_TIMEOUT)

        listener = threading.Thread(target=self._listen_loop, daemon=True)
        heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.threads = [listener, heartbeat]
        listener.start()
        heartbeat.start()
        self.send_hello()

    def stop(self):
        if self.sock is None or self.stopping.is_set():
            return
        self._send_everywhere(protocol.encode("BYE", self.name, self._bye_payload()))
        self.log("UDP BYE broadcast sent")
        self.stopping.set()
        self.sock.close()
        for t in self.threads:
            t.join(timeout=2)

    # ------------------------------------------------------------------
    def _my_address(self):
        return "%s:%d" % (self.my_ip, self.tcp_port)

    def _same_host(self, a, b):
        """Two addresses of the same computer? Several instances on ONE PC may be seen via the LAN IP
        or via 127.0.0.1, which is the same machine."""
        local = (config.LOOPBACK_ADDRESS, self.my_ip)
        return a == b or (a in local and b in local)

    @staticmethod
    def _bye_text(name, address, when):
        return ("NetChat-bye-v1|%s|%s|%s" % (name, address, when)).encode("utf-8")

    def _bye_payload(self):
        """'ip:port', plus ';t=<unix time>;sig=<hex>' when we have an identity key. Older versions
        ignore everything in a BYE payload, so the extra fields are backward compatible."""
        address = self._my_address()
        if self.signer is None:
            return address
        when = "%d" % time.time()
        signature = self.signer(self._bye_text(self.name, address, when))
        return "%s;t=%s;sig=%s" % (address, when, signature.hex())

    def send_hello(self):
        self._send_everywhere(protocol.encode("HELLO", self.name, self._my_address()))
        self.log("UDP HELLO broadcast sent (%s)" % self._my_address())

    def _send_everywhere(self, text):
        data = text.encode("utf-8")
        try:
            self.sock.sendto(data, (config.BROADCAST_ADDRESS, self.udp_port))
        except OSError as e:
            # Some networks (or having no network) make broadcast fail. Do not crash.
            self.log("Broadcast failed: %s" % e)
            if not self.broadcast_warned:
                self.broadcast_warned = True
                self.on_status("Broadcast failed (%s). Discovery may not work on this network." % e)
            # Fallback: talk to this computer itself, so instances on the same machine
            # can still find each other even when the network refuses broadcasts.
            try:
                self.sock.sendto(data, (config.LOOPBACK_ADDRESS, self.udp_port))
            except OSError as e:
                self.log("Loopback fallback failed too: %s" % e)

    # ------------------------------------------------------------------
    def _listen_loop(self):
        while not self.stopping.is_set():
            try:
                data, addr = self.sock.recvfrom(config.BUFFER_SIZE)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Windows reports "ICMP port unreachable" from an earlier sendto()
                # as an error on the next recvfrom(). It is harmless: ignore it.
                continue
            except OSError:
                if self.stopping.is_set():
                    break
                time.sleep(0.2)
                continue
            self._handle_datagram(data, addr)

    def _handle_datagram(self, data, addr):
        try:
            msg_type, sender, _ts, payload = protocol.decode(data.decode("utf-8"))
        except (UnicodeDecodeError, protocol.ProtocolError):
            self.log("Ignored a malformed UDP datagram from %s:%d" % addr)
            return

        # We hear our own broadcasts too. Peer names are the identity of a peer,
        # so a packet carrying our own name is ignored.
        if sender == self.name:
            return

        if msg_type in ("HELLO", "REPLY"):
            try:
                ip, port_text = payload.rsplit(":", 1)
                port = int(port_text)
            except ValueError:
                self.log("Ignored %s from %s with bad payload %r" % (msg_type, sender, payload))
                return
            # The IP inside the packet is what the sender GUESSES is its address. On a PC with several
            # network cards (VPN, VirtualBox, WSL, hotspot) that guess can be a card we cannot reach.
            # The address the packet really came from is, by definition, one that reaches the sender.
            if ip != addr[0]:
                self.log("%s advertises %s but its packet came from %s: using %s" % (sender, ip, addr[0], addr[0]))
                ip = addr[0]
            verdict = self.table.claim(sender, ip, port, self._same_host)
            if verdict == "conflict":
                self.log("Ignored %s from %s at %s:%d: that name is already used by another live peer"
                         % (msg_type, sender, ip, port))
                if (sender, ip, port) not in self.conflicts_reported:
                    self.conflicts_reported.add((sender, ip, port))
                    self.on_conflict(sender, "%s:%d" % (ip, port))
                return
            is_new = verdict == "new"
            self.log("UDP %s received from %s (%s:%d)" % (msg_type, sender, ip, port))
            if msg_type == "HELLO":
                # Answer directly (unicast) to whoever sent the HELLO.
                reply = protocol.encode("REPLY", self.name, self._my_address())
                try:
                    self.sock.sendto(reply.encode("utf-8"), addr)
                    self.log("UDP REPLY sent to %s" % sender)
                except OSError as e:
                    self.log("Could not send REPLY to %s: %s" % (sender, e))
            if is_new:
                if msg_type == "HELLO":
                    # We just met someone new. UDP can lose the unicast REPLY (and when two
                    # instances share one UDP port on one Windows PC, only ONE of them gets a
                    # unicast packet). So announce ourselves again by broadcast: that reaches
                    # everybody. It cannot loop, because this only happens for peers
                    # that are new to us.
                    self.send_hello()
                self.on_change()

        elif msg_type == "BYE":
            self._handle_bye(sender, payload, addr)

    def _handle_bye(self, sender, payload, addr):
        """Anyone can send a UDP packet with any name in it, so a BYE is believed only if
          * it carries a valid Ed25519 signature by the key we pinned for that name, with a newer
            time than the last BYE (replay protection), or
          * we know no key for that name yet and it comes from the address in our table."""
        known = self.table.get(sender)
        if known is None:
            return
        address, _, extras = payload.partition(";")
        fields = dict(part.split("=", 1) for part in extras.split(";") if "=" in part)
        try:
            when = int(fields.get("t", "0"))
            signature = bytes.fromhex(fields.get("sig", ""))
        except ValueError:
            when, signature = 0, b""
        verdict = None
        if self.verifier is not None:
            verdict = self.verifier(sender, self._bye_text(sender, address, fields.get("t", "")), signature)
        if verdict is False:
            reason = "bad or missing signature"
        elif verdict is True and when <= self.last_bye.get(sender, 0):
            reason = "replayed"
        elif verdict is True and abs(time.time() - when) > config.BYE_MAX_AGE:
            reason = "too old (or the clocks differ a lot)"
        elif verdict is None and not (self._same_host(addr[0], known[0]) and address.endswith(":%d" % known[1])):
            reason = "it came from %s, not from %s's address" % (addr[0], sender)
        else:
            reason = ""
        if reason:
            self.log("Ignored a BYE for %s: %s" % (sender, reason))
            return
        if verdict is True:
            self.last_bye[sender] = when
        if self.table.remove(sender):
            self.log("UDP BYE received from %s" % sender)
            self.on_change()

    # ------------------------------------------------------------------
    def _heartbeat_loop(self):
        # Event.wait(n) sleeps n seconds but returns early (True) when stop is requested.
        while not self.stopping.wait(config.HEARTBEAT_INTERVAL):
            self.send_hello()
            stale = self.table.remove_stale(config.PEER_TIMEOUT)
            if stale:
                self.log("Timed out (no heartbeat): %s" % ", ".join(stale))
                self.on_change()
