"""
peer.py - the heart of NetChat: every instance is BOTH a TCP server and a TCP client.

  * Server side: a listening socket (bind -> listen -> accept). Every accepted
    connection gets a reader thread.
  * Client side: to send we connect() to the other peer's server port. We keep ONE
    connection per peer and reuse it.
  * Every connection works in BOTH directions. The side that dialed sends chat
    messages / files; the side that accepted answers on the SAME socket with ACKs,
    PONGs and file accept/decline. That is why every connection has a reader thread.

Frames (see protocol.py) are length-prefixed JSON. Discovery (UDP) is in discovery.py.

This file knows nothing about windows. It reports what happens through callbacks
(on_event, on_status, ...) that the GUI turns into queue entries.
"""

import collections
import os
import re
import socket
import threading
import time
import uuid

from . import config
from . import crypto
from . import protocol
from .connection import Connection
from .discovery import Discovery, PeerTable, get_local_ip
from .filetransfer import FileManager
from .storage import Database


def _ignore(*args, **kwargs):
    """Default for callbacks nobody asked for."""


def explain_error(error):
    """A human reason for a failed connection. The two classic causes look different:
    a time-out means packets vanish (firewall), 'refused' means the computer answered but no
    NetChat listens on that port."""
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "no answer: a firewall on that computer is probably blocking the TCP port"
    if isinstance(error, ConnectionRefusedError):
        return "refused: NetChat is not listening on that port there"
    return str(error)


class HandshakeError(OSError):
    """The HI exchange failed. `reason` is 'version', 'auth', 'identity', 'name' or 'protocol'."""

    def __init__(self, message, reason="protocol"):
        super().__init__(message)
        self.reason = reason


class Peer:
    def __init__(self, name, tcp_port, udp_port, log,
                 on_message=_ignore, on_peers_changed=_ignore, on_status=_ignore, on_event=_ignore,
                 db=None, passphrase="", download_dir=None, identity_path=None, encrypt=True):
        self.name = name
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.log = log.log                        # log(text) writes one timestamped line
        self.on_message = on_message              # on_message(sender, text, timestamp)  (simple API)
        self.on_peers_changed = on_peers_changed  # on_peers_changed()
        self.on_status = on_status                # on_status(text)
        self.on_event = on_event                  # on_event(kind, info_dict)  (everything else)
        self.db = db if db is not None else Database(":memory:", name)
        self.passphrase = passphrase
        self.download_dir = download_dir or os.path.join(os.path.expanduser("~"), *config.DOWNLOAD_SUBDIR)

        # My long-term identity key (None if encryption is off or the `cryptography` package is missing).
        self.identity = crypto.Identity(identity_path) if encrypt and crypto.AVAILABLE else None
        self.fingerprint = self.identity.fingerprint if self.identity else ""

        self.my_ip = get_local_ip()
        self.table = PeerTable()
        self.discovery = Discovery(name, self.my_ip, tcp_port, udp_port, self.table,
                                   self.log, self._peers_changed, self._status,
                                   signer=self.identity.sign if self.identity else None,
                                   verifier=self._verify_peer_signature if crypto.AVAILABLE else None,
                                   on_conflict=self._name_conflict)

        self.stopping = threading.Event()
        self.server = None
        self.threads = []                         # every thread we start, so stop() can join them
        self.threads_lock = threading.Lock()

        # name -> Connection we use to SEND to that peer. conn_lock protects both dicts.
        self.connections = {}
        self.dial_locks = {}                      # one lock per peer: only one dial at a time
        self.conn_lock = threading.Lock()
        # connections we ACCEPTED. Kept so we can close them on exit.
        self.incoming = []

        self.state_lock = threading.Lock()        # protects the small dicts below
        self.pending_acks = {}                    # (msg id, recipient) -> time we give up waiting
        self.seen_ids = collections.OrderedDict() # ids of messages already delivered to the GUI
        self.known_online = set()                 # who was online at the last peers change
        self.last_typing = {}                     # peer -> time of our last TYPING frame
        self.rtt = {}                             # peer -> (round trip seconds, measured at)
        self.flush_locks = {}                     # peer -> lock, keeps queued messages in order
        self.flush_again = set()                  # peers whose queue changed while a flush was running
        self.connected_info = {}                  # peer -> (encrypted, fingerprint)
        self.send_queues = {}                     # peer -> deque of (msg id, fields) for deliver_async()
        self.reconnecting = set()                 # peers with a running reconnection worker
        self.manual = {}                          # (ip, port) added by hand -> peer name (None until connected)
        self.files = FileManager(self)            # file transfers (see filetransfer.py)
        if self.passphrase and self.identity is None:
            self.log("WARNING: a passphrase is set but encryption is unavailable; no peer will accept us")

    # small wrappers so the callbacks can be swapped by the GUI/CLI
    def _peers_changed(self):
        """Discovery calls this (from the UDP thread) when somebody appeared or vanished."""
        online = set(self.table.names())
        with self.state_lock:
            appeared = online - self.known_online
            self.known_online = online
        for name in sorted(appeared):
            self.files.peer_online(name)          # resume interrupted file transfers to it
            # Store and forward: the peer is back, so deliver what we kept for it. Doing it in
            # a new thread keeps the UDP listener free (connecting can take seconds).
            if self.db.outbox_count(name):
                self.log("%s is back, delivering %d queued message(s)" % (name, self.db.outbox_count(name)))
                self._spawn(self.flush_outbox, name)
        self.on_peers_changed()

    def _status(self, text):
        self.on_status(text)

    def _name_conflict(self, name, address):
        """Discovery heard a second computer using the name of a peer that is still online."""
        self._status("Another computer (%s) also calls itself %s. It is ignored; messages keep going to the "
                     "first one. If you expected this, ask one of you to pick a different name." % (address, name))
        self._event("name_conflict", peer=name, address=address)

    # ------------------------------------------------------------------
    # peer identities: trust on first use (TOFU)
    # ------------------------------------------------------------------
    def _verify_peer_signature(self, name, message, signature):
        """For discovery (signed BYE): True / False, or None if we have no key for `name` yet."""
        known = self.db.peer_key(name)
        if known is None:
            return None
        return crypto.verify(bytes.fromhex(known), message, signature)

    def _check_identity(self, name, ident, pin):
        """Names are chosen freely, so a name alone proves nothing. The FIRST identity key seen for a
        name is remembered (pinned); later a different key under that name is refused, because it is
        either a different computer using the same name or an impersonator. `pin=False` only
        checks (used before the handshake is complete, so a half-finished handshake cannot pin)."""
        presented = ident.hex()
        known = self.db.pin_peer_key(name, presented) if pin else self.db.peer_key(name)
        if known is None or known == presented:
            return
        old_fp, new_fp = crypto.fingerprint_of(bytes.fromhex(known)), crypto.fingerprint_of(ident)
        self._event("identity", peer=name, known=old_fp, presented=new_fp, ident=presented)
        raise HandshakeError("%s presented a different identity key (fingerprint %s, remembered %s). Another "
                             "computer may be using that name" % (name, new_fp, old_fp), "identity")

    def _require_key_if_pinned(self, name):
        """We can encrypt and know a key for `name`, but this connection is not encrypted: refuse,
        otherwise an impersonator would simply skip the key exchange (a downgrade attack)."""
        if self.identity is not None and self.db.peer_key(name) is not None:
            raise HandshakeError("%s used encryption before but this connection offers none (impersonation or "
                                 "--no-encryption on that side)" % name, "identity")

    def _check_address(self, conn):
        """Without encryption nothing proves who dialed us. At least the connection must come from the
        computer that discovery knows under that name."""
        known = self.table.get(conn.remote_name)
        if known is not None and not self.discovery._same_host(known[0], conn.address[0]):
            raise HandshakeError("a connection from %s claimed to be %s, who is at %s"
                                 % (conn.address[0], conn.remote_name, known[0]), "identity")

    def trust_identity(self, name, ident_hex):
        """The user compared fingerprints and accepts a NEW key for `name` (e.g. NetChat reinstalled)."""
        self.db.set_peer_key(name, ident_hex)
        self.log("New identity key for %s accepted by the user (%s)"
                 % (name, crypto.fingerprint_of(bytes.fromhex(ident_hex))))

    def _event(self, kind, **info):
        try:
            self.on_event(kind, info)
        except Exception as e:                    # a broken callback must never kill a network thread
            self.log("Error in event callback: %r" % e)

    def _spawn(self, target, *args):
        """Run target(*args) in a daemon thread that stop() can join."""
        thread = threading.Thread(target=target, args=args, daemon=True)
        with self.threads_lock:
            self.threads = [t for t in self.threads if t.is_alive()]
            self.threads.append(thread)
        thread.start()
        return thread

    # ------------------------------------------------------------------
    # start / stop
    # ------------------------------------------------------------------
    def start(self):
        """Open the TCP server and start discovery. Raises OSError if a port is busy."""
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt":
            # On Windows SO_REUSEADDR would let a second program grab our port
            # too, hiding the "port already in use" error. EXCLUSIVEADDRUSE forbids that.
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            # On Linux/Mac, SO_REUSEADDR lets us restart quickly after closing.
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.server.bind(("", self.tcp_port))   # claim the port on all interfaces
            self.server.listen(5)                   # start waiting for connections
        except OSError:
            self.server.close()
            raise
        self.server.settimeout(config.SOCKET_TIMEOUT)   # so accept() can notice "stopping"
        self.log("TCP server listening on port %d" % self.tcp_port)

        self.db.fail_stale_sending()
        self.files.load()                       # transfers interrupted last time resume when their peer is back
        self._spawn(self._accept_loop)
        self._spawn(self._housekeeping_loop)

        try:
            self.discovery.start()
        except OSError:
            self.stop()
            raise

    def stop(self):
        """Say BYE, then close every socket and let the threads finish."""
        if self.stopping.is_set():
            return
        self.discovery.stop()               # broadcasts BYE first
        self.stopping.set()
        if self.server is not None:
            self.server.close()
        self.files.stop()
        with self.conn_lock:
            connections = list(self.connections.values()) + list(self.incoming)
            self.connections.clear()
            self.incoming.clear()
        for conn in connections:
            conn.close()
        with self.threads_lock:
            threads = list(self.threads)
        # One deadline for ALL threads: closing the window must never hang for 2 s per thread.
        deadline = time.time() + config.STOP_TIMEOUT
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.time()))
        self.log("Shut down, all sockets closed")

    def peer_names(self):
        return self.table.names()

    def set_passphrase(self, passphrase):
        """Change the shared passphrase while running. Existing connections were encrypted with
        the old one, so they are closed; the next message makes a new connection with the new one."""
        self.passphrase = passphrase
        with self.conn_lock:
            connections = list(self.connections.values()) + list(self.incoming)
            self.connections.clear()
            self.incoming.clear()
        for conn in connections:
            conn.close()
        with self.state_lock:
            self.connected_info.clear()

    # ------------------------------------------------------------------
    # server side: accept connections, handshake, read frames
    # ------------------------------------------------------------------
    def _accept_loop(self):
        while not self.stopping.is_set():
            try:
                sock, addr = self.server.accept()   # blocks until someone connects
            except socket.timeout:
                continue
            except OSError:
                break                               # server socket was closed
            self.log("TCP connection accepted from %s:%d" % addr)
            conn = Connection(sock, addr, outgoing=False)
            with self.conn_lock:
                self.incoming.append(conn)
            # One thread per connection, so one slow peer cannot block the others.
            self._spawn(self._serve_incoming, conn)

    def _serve_incoming(self, conn):
        """The accepting side of the handshake, then the normal read loop."""
        try:
            deadline = time.time() + config.HANDSHAKE_TIMEOUT
            hello = conn.read_frame(self.stopping, deadline)
            self._check_hello(conn, hello)
            handshake, reply = None, self._hello_fields()
            if self.identity is not None and "eph" in hello.fields:
                handshake = crypto.Handshake(self.identity, self.passphrase, "listener")
                try:
                    reply.update(handshake.listener_accept(hello.fields))
                except crypto.CryptoError as e:
                    raise HandshakeError(str(e), "auth")
                self._check_identity(conn.remote_name, handshake.remote_identity, pin=False)
            elif self.passphrase:
                raise HandshakeError("a passphrase is set, so the connection must be encrypted", "auth")
            else:
                self._require_key_if_pinned(conn.remote_name)
                self._check_address(conn)
            conn.send("HI", reply)
            if handshake is not None:
                auth = conn.read_frame(self.stopping, deadline)
                if auth.type == "ERROR":
                    raise HandshakeError("the other side refused: %s" % auth.get("text", "?"), auth.get("code", "auth"))
                if auth.type != "AUTH":
                    raise HandshakeError("expected AUTH, got %s" % auth.type)
                try:
                    handshake.listener_verify_auth(auth.fields)
                except crypto.CryptoError as e:
                    raise HandshakeError(str(e), "auth")
                self._enable_encryption(conn, handshake)
                # Only now is the handshake complete: pin the key (re-checked, another handshake for the
                # same name may have pinned one meanwhile). An ERROR sent now is already encrypted.
                self._check_identity(conn.remote_name, handshake.remote_identity, pin=True)
        except protocol.VersionError as e:
            self._reject(conn, "version", str(e))
            return
        except HandshakeError as e:
            self._reject(conn, e.reason, str(e))
            return
        except (OSError, protocol.FrameError, socket.timeout) as e:
            self.log("Handshake from %s:%d failed: %s" % (conn.address[0], conn.address[1], e))
            self._forget(conn)
            return
        self._announce_connection(conn)
        self._read_loop(conn)

    def _tell_why(self, conn, code, text):
        """Send an ERROR frame (best effort) so the other side learns why we are hanging up."""
        try:
            conn.send("ERROR", {"code": code, "text": text})
        except (OSError, protocol.FrameError):
            pass

    def _reject(self, conn, code, text):
        """Tell the other side why we hang up (best effort), log it, close."""
        self.log("Rejected %s:%d: %s" % (conn.address[0], conn.address[1], text))
        self._tell_why(conn, code, text)
        self._forget(conn)
        if code in ("version", "auth", "identity"):
            self._status("%s was rejected: %s" % (conn.remote_name or "A connection", text))

    def _read_loop(self, conn):
        """Read frames from `conn` until it closes, handing each one to _on_frame()."""
        try:
            while not self.stopping.is_set():
                frame = conn.read_frame(self.stopping)
                try:
                    self._on_frame(conn, frame)
                except Exception as e:          # a bug in one handler must not kill the connection
                    self.log("Error handling %s frame: %r" % (frame.type, e))
        except protocol.FrameError as e:
            self.log("Dropping connection with %s: bad frame (%s)" % (conn.remote_name or conn.address[0], e))
        except (ConnectionError, socket.timeout):
            pass
        finally:
            self._forget(conn)

    def _forget(self, conn):
        """Close `conn` and remove it from our tables."""
        was_open = not conn.closed
        conn.close()
        with self.conn_lock:
            if conn in self.incoming:
                self.incoming.remove(conn)
            if self.connections.get(conn.remote_name) is conn:
                del self.connections[conn.remote_name]
        if was_open and conn.remote_name:
            self.log("TCP connection with %s closed" % conn.remote_name)
        self.files.connection_lost(conn)
        if conn.outgoing and conn.remote_name and not self.stopping.is_set() and not getattr(conn, "redundant", False):
            self._requeue_unacked(conn.remote_name)
            self._spawn(self._reconnect, conn.remote_name)

    def _reconnect(self, name):
        """A connection we dialed broke. Try to build a new one a few times, with growing pauses
        (bounded: RECONNECT_DELAYS), so the lock icon, queued messages and file transfers come back
        by themselves after a short network hiccup. Stops early if the peer left (BYE / time-out)."""
        with self.state_lock:
            if name in self.reconnecting:
                return
            self.reconnecting.add(name)
        try:
            for attempt, delay in enumerate(config.RECONNECT_DELAYS, 1):
                if self.stopping.wait(delay):
                    return
                address = self.table.get(name)
                if address is None:
                    return                                  # it left: discovery brings it back later
                with self.conn_lock:
                    conn = self.connections.get(name)
                    if conn is not None and not conn.closed:
                        return                              # somebody else reconnected already
                try:
                    self._get_connection(name, address)
                except HandshakeError as e:
                    self.log("Reconnecting to %s failed: %s" % (name, e))
                    if e.reason in ("version", "auth", "identity"):
                        return                              # waiting will not help
                except OSError as e:
                    self.log("Reconnecting to %s, attempt %d of %d failed: %s"
                             % (name, attempt, len(config.RECONNECT_DELAYS), explain_error(e)))
                else:
                    self.log("Reconnected to %s (attempt %d)" % (name, attempt))
                    if self.db.outbox_count(name):
                        self._spawn(self.flush_outbox, name)
                    return
            self.log("Gave up reconnecting to %s after %d attempts" % (name, len(config.RECONNECT_DELAYS)))
        finally:
            with self.state_lock:
                self.reconnecting.discard(name)

    def _requeue_unacked(self, name):
        """The connection to `name` died. Messages sent on it that were never ACKed may be lost
        (TCP throws away unread data when a connection is reset), so put them back in the queue
        and send them again on a new connection. The receiver ignores duplicates by id."""
        with self.state_lock:
            lost = [key for key in self.pending_acks if key[1] == name]
            for key in lost:
                del self.pending_acks[key]
        for msg_id, _ in lost:
            self._message_status(msg_id, name, "queued")
        if lost and name in self.table.names():
            self.log("Connection to %s broke with %d unacknowledged message(s), sending them again" % (name, len(lost)))
            self._spawn(self.flush_outbox, name)

    # ------------------------------------------------------------------
    # handshake
    # ------------------------------------------------------------------
    def _hello_fields(self):
        # "port": our TCP server port, so a peer that cannot hear our UDP HELLO (we added it by IP)
        # can still connect back to us. Older versions ignore the field.
        fields = {"name": self.name, "pass": bool(self.passphrase), "port": self.tcp_port}
        return fields

    def _check_hello(self, conn, frame):
        """Validate the other side's HI frame and remember who it is."""
        if frame.type == "ERROR":
            raise HandshakeError("the other side refused: %s" % frame.get("text", "?"), frame.get("code", "protocol"))
        if frame.type != "HI":
            raise HandshakeError("expected HI, got %s" % frame.type)
        name = frame.get("name")
        if not isinstance(name, str) or not name or len(name) > config.MAX_NAME_LEN:
            raise HandshakeError("bad name in HI", "name")
        if name == self.name:
            raise HandshakeError("a peer with our own name %r connected" % name, "name")
        conn.remote_name = name
        port = frame.get("port")
        conn.remote_port = port if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536 else None
        # Both sides must agree on whether a shared passphrase is in use, otherwise say so clearly
        # now instead of failing later with a confusing "key confirmation failed".
        if bool(frame.get("pass")) != bool(self.passphrase):
            raise HandshakeError("passphrase mismatch: %s" % (
                "that peer requires a shared passphrase and we have none" if frame.get("pass")
                else "we use a shared passphrase and that peer does not"), "auth")

    def _enable_encryption(self, conn, handshake):
        """The handshake succeeded: every following frame on `conn` is an AES-GCM record."""
        conn.secure = handshake.channel()
        conn.encrypted = True
        conn.fingerprint = handshake.remote_fingerprint

    def _announce_connection(self, conn):
        with self.state_lock:
            self.connected_info[conn.remote_name] = (conn.encrypted, conn.fingerprint)
        self._event("secure", peer=conn.remote_name, encrypted=conn.encrypted, fingerprint=conn.fingerprint)
        if not conn.outgoing and conn.remote_port and self.table.get(conn.remote_name) is None:
            # Somebody we never discovered connected to us (UDP blocked, it added us by IP): remember
            # where its server is, so we can send to it too.
            self.table.add(conn.remote_name, conn.address[0], conn.remote_port)
            self.log("%s connected directly; its server is at %s:%d" % (conn.remote_name, conn.address[0], conn.remote_port))
            self._peers_changed()
        self.files.peer_online(conn.remote_name)

    def _dial(self, name, address):
        """Open a new connection to a peer and do the handshake. Raises OSError on any failure.
        name=None: we do not know who is there yet (connecting by IP); the HI tells us."""
        sock = socket.create_connection(address, timeout=config.CONNECT_TIMEOUT)
        conn = Connection(sock, address, outgoing=True)
        try:
            deadline = time.time() + config.HANDSHAKE_TIMEOUT
            handshake, hello = None, self._hello_fields()
            if self.identity is not None:
                handshake = crypto.Handshake(self.identity, self.passphrase, "dialer")
                hello.update(handshake.dialer_hello())
            conn.send("HI", hello)
            reply = conn.read_frame(self.stopping, deadline)
            self._check_hello(conn, reply)
            if name is None:
                name = conn.remote_name
            if conn.remote_name != name:
                raise HandshakeError("expected %s at %s:%d but found %s" % (name, address[0], address[1], conn.remote_name), "name")
            if handshake is not None and "eph" in reply.fields:
                try:
                    auth = handshake.dialer_finish(reply.fields)
                except crypto.CryptoError as e:
                    self._tell_why(conn, "auth", str(e))         # the other side is waiting for our AUTH
                    raise HandshakeError(str(e), "auth")
                try:
                    self._check_identity(name, handshake.remote_identity, pin=True)
                except HandshakeError as e:
                    self._tell_why(conn, "identity", "your identity key is not the one I know for %s" % name)
                    raise
                conn.send("AUTH", auth)                          # still plaintext: it only carries a MAC
                self._enable_encryption(conn, handshake)         # everything after this is encrypted
            elif self.passphrase:
                raise HandshakeError("a passphrase is set, but %s cannot encrypt" % name, "auth")
            else:
                self._require_key_if_pinned(name)
        except protocol.VersionError as e:
            conn.close()
            raise HandshakeError(str(e), "version")
        except (OSError, protocol.FrameError, socket.timeout) as e:
            conn.close()
            if isinstance(e, HandshakeError):
                raise
            raise HandshakeError("handshake with %s failed: %s" % (name, e))
        self.log("TCP connected to %s (%s:%d)" % (name, address[0], address[1]))
        self._announce_connection(conn)
        conn.thread = self._spawn(self._read_loop, conn)
        return conn

    # ------------------------------------------------------------------
    # frames arriving (in the reader thread of a connection)
    # ------------------------------------------------------------------
    def _on_frame(self, conn, frame):
        kind = frame.type
        if kind == "MSG":
            self._handle_msg(conn, frame)
        elif kind == "ACK":
            self._handle_ack(conn, frame)
        elif kind == "TYPING":
            self._event("typing", peer=conn.remote_name)
        elif kind == "PING":
            self._reply(conn, "PONG", {"id": frame.get("id"), "sent": frame.get("sent")})
        elif kind == "PONG":
            self._handle_pong(conn, frame)
        elif kind.startswith("FILE_"):
            self.files.on_frame(conn, frame)
        elif kind == "ERROR":
            self.log("%s reported an error: %s" % (conn.remote_name, frame.get("text")))
            self._status("%s: %s" % (conn.remote_name, frame.get("text")))
            self._forget(conn)
        else:
            self.log("Ignored unexpected %s frame from %s" % (kind, conn.remote_name))

    def _reply(self, conn, frame_type, fields=None):
        try:
            conn.send(frame_type, fields)
        except (OSError, protocol.FrameError) as e:
            self.log("Could not send %s to %s: %s" % (frame_type, conn.remote_name, e))

    def _handle_msg(self, conn, frame):
        msg_id, text = frame.get("id"), frame.get("text")
        group = self._clean_group(frame.get("group"))
        if (not isinstance(msg_id, str) or not msg_id or len(msg_id) > 64
                or not isinstance(text, str) or len(text) > config.MAX_TEXT_LEN * 4
                or (frame.get("group") is not None and group is None)):
            self.log("Ignored a malformed MSG from %s" % conn.remote_name)
            return
        sender = conn.remote_name
        # ACK first, always: a duplicate means our earlier ACK got lost, so the sender needs another.
        self._reply(conn, "ACK", {"id": msg_id})
        with self.state_lock:
            # Keyed by (sender, id): one peer must not be able to suppress another peer's message by
            # sending its id first.
            if (sender, msg_id) in self.seen_ids:
                return                          # a retry of a message we already delivered
            self.seen_ids[(sender, msg_id)] = True
            while len(self.seen_ids) > config.SEEN_IDS_KEEP:
                self.seen_ids.popitem(last=False)
        self.log("Message received from %s" % sender)
        ts = frame.get("ts") if isinstance(frame.get("ts"), str) else time.strftime("%H:%M:%S")
        self.on_message(sender, text, ts)
        self._event("message", peer=sender, id=msg_id, text=text, ts=ts, group=group)

    @staticmethod
    def _clean_group(group):
        """Return a validated {id, name, members} dict, or None if the input is not one."""
        if not isinstance(group, dict):
            return None
        gid, gname, members = group.get("id"), group.get("name"), group.get("members")
        if not (isinstance(gid, str) and 0 < len(gid) <= 64 and isinstance(gname, str) and 0 < len(gname) <= 60):
            return None
        if gid == "everyone":
            return {"id": gid, "name": "Everyone", "members": []}
        if (not isinstance(members, list) or not 0 < len(members) <= 100
                or not all(isinstance(m, str) and 0 < len(m) <= config.MAX_NAME_LEN for m in members)):
            return None
        return {"id": gid, "name": gname, "members": members}

    def _handle_ack(self, conn, frame):
        msg_id, peer = frame.get("id"), conn.remote_name
        if not isinstance(msg_id, str):
            return
        # Order matters: first remove the outbox row, THEN clear "pending". The other way round,
        # there is a moment where the row looks like "old and not in flight" and would make a
        # new message wait behind it.
        self.db.outbox_remove(msg_id, peer)
        with self.state_lock:
            self.pending_acks.pop((msg_id, peer), None)
        self._event("msg_status", id=msg_id, peer=peer, status="delivered")

    def _handle_pong(self, conn, frame):
        sent = frame.get("sent")
        if isinstance(sent, (int, float)):
            seconds = max(0.0, time.perf_counter() - sent)
            with self.state_lock:
                self.rtt[conn.remote_name] = (seconds, time.time())
            self.table.touch(conn.remote_name)            # alive, even if its UDP HELLOs do not reach us
            self._event("rtt", peer=conn.remote_name, rtt=seconds)

    # ------------------------------------------------------------------
    # client side: connections and sending
    # ------------------------------------------------------------------
    def _get_connection(self, name, address):
        """Reuse the open connection to `name`, or dial a new one."""
        with self.conn_lock:
            conn = self.connections.get(name)
            if conn is not None and not conn.closed:
                return conn
            dial_lock = self.dial_locks.setdefault(name, threading.Lock())
        with dial_lock:                             # two threads must not dial the same peer twice
            with self.conn_lock:
                conn = self.connections.get(name)
                if conn is not None and not conn.closed:
                    return conn
            if address is None:
                raise OSError("%s is not online" % name)
            if conn is not None:
                self.log("Connection to %s was broken, reconnecting" % name)
            conn = self._dial(name, address)
            with self.conn_lock:
                self.connections[name] = conn
            return conn

    def _drop_connection(self, name):
        with self.conn_lock:
            conn = self.connections.pop(name, None)
        if conn is not None:
            conn.close()

    def _send_frame(self, name, frame_type, fields=None, data=b""):
        """Send one frame to `name`, connecting if needed. Returns the Connection used.

        Tries twice: the 2nd try builds a brand-new connection, which fixes a connection
        that broke since we last used it. Raises OSError if both tries fail."""
        address = self.table.get(name)
        error = None
        for attempt in (1, 2):
            try:
                conn = self._get_connection(name, address)
                conn.send(frame_type, fields, data)
                return conn
            except HandshakeError:
                raise                               # retrying cannot fix a version/passphrase problem
            except (OSError, protocol.FrameError) as e:
                error = e
                self.log("Send to %s failed (try %d): %s" % (name, attempt, e))
                self._drop_connection(name)
        raise error

    # ---- connecting by IP (when UDP discovery does not work) ---------------
    @staticmethod
    def parse_address(text):
        """'192.168.1.7', '192.168.1.7:5002' or 'laptop:5002' -> (host, port). Raises ValueError."""
        text = (text or "").strip()
        host, colon, port_text = text.rpartition(":")
        if not colon:
            host, port_text = text, str(config.DEFAULT_TCP_PORT)
        if not host or not port_text.isdigit() or not 0 < int(port_text) < 65536 or not re.fullmatch(r"[\w.\-]+", host):
            raise ValueError("expected IP or IP:port, for example 192.168.1.7:5000")
        return host, int(port_text)

    def connect_to(self, host, port=config.DEFAULT_TCP_PORT):
        """Connect to the NetChat server at host:port and add whoever answers to the peer list.
        All the usual checks apply (handshake, encryption, identity key). Returns the peer's name;
        raises OSError (HandshakeError) with a readable reason."""
        ip = socket.gethostbyname(host)
        if port == self.tcp_port and self.discovery._same_host(ip, self.my_ip) or (ip, port) == ("127.0.0.1", self.tcp_port):
            raise OSError("that address is this NetChat itself")
        conn = self._dial(None, (ip, port))
        name = conn.remote_name
        with self.conn_lock:
            current = self.connections.get(name)
            if current is None or current.closed:
                self.connections[name] = current = conn
        if current is not conn:
            conn.redundant = True                    # we already had one: this extra one goes quietly
            conn.close()
        with self.state_lock:
            self.manual[(ip, port)] = name
        self.table.add(name, ip, port)
        self.log("Connected by address to %s at %s:%d" % (name, ip, port))
        self._peers_changed()
        return name

    def connect_async(self, text):
        """connect_to() in the background; the result is reported with on_status (GUI / CLI)."""
        try:
            host, port = self.parse_address(text)
        except ValueError as e:
            self._status("Cannot connect: %s" % e)
            return

        def work():
            try:
                name = self.connect_to(host, port)
            except (OSError, protocol.FrameError) as e:
                with self.state_lock:
                    self.manual.setdefault((host, port), None)   # keep trying in the background
                self._status("Could not connect to %s:%d: %s" % (host, port, explain_error(e)))
            else:
                self._status("Connected to %s at %s:%d" % (name, host, port))
        self._spawn(work)

    def _retry_manual(self):
        """Every PING round: addresses added by hand whose peer is not online are tried once again."""
        with self.state_lock:
            manual = dict(self.manual)
        online = set(self.table.names())
        for (host, port), name in manual.items():
            if self.stopping.is_set():
                return
            if name in online:
                continue
            try:
                self.connect_to(host, port)
            except (OSError, protocol.FrameError) as e:
                self.log("Address %s:%d still unreachable: %s" % (host, port, explain_error(e)))

    def ensure_connection(self, name):
        """Connect to a peer in the background (so the lock icon / fingerprint can show up)."""
        def work():
            try:
                self._get_connection(name, self.table.get(name))
            except OSError as e:
                self.log("Could not connect to %s in the background: %s" % (name, e))
        self._spawn(work)

    # ---- chat messages: ACK, offline queue -------------------------------
    def deliver(self, recipient, msg_id, fields):
        """Send one MSG to one recipient, with delivery tracking. Returns 'sent', 'queued' or 'failed'.

        The message is written to the outbox FIRST and removed only when the ACK
        arrives. So a message survives a failed connection, a crash and a restart, and
        is simply sent again later (the receiver drops duplicates by id).
        """
        fields = dict(fields)
        self.db.outbox_add(msg_id, recipient, fields)
        if self._has_older_waiting(recipient, msg_id):
            # Something older is still waiting for this peer: keep the ORDER by queuing behind it.
            self._spawn(self.flush_outbox, recipient)
            self._message_status(msg_id, recipient, "queued")
            return "queued"
        return self._send_queued(recipient, msg_id, fields)

    def deliver_async(self, recipient, msg_id, fields):
        """deliver() in a background thread (for the GUI: connecting may take seconds). Messages to
        the same recipient are handled strictly in the order of these calls by ONE worker per
        recipient; a separate thread per message could overtake the previous one."""
        with self.state_lock:
            waiting = self.send_queues.get(recipient)
            start = waiting is None
            if start:
                waiting = self.send_queues[recipient] = collections.deque()
            waiting.append((msg_id, dict(fields)))
        if start:
            self._spawn(self._delivery_worker, recipient)

    def _delivery_worker(self, recipient):
        while True:
            with self.state_lock:
                waiting = self.send_queues[recipient]
                if not waiting or self.stopping.is_set():
                    del self.send_queues[recipient]     # the next deliver_async() starts a new worker
                    return
                msg_id, fields = waiting.popleft()
            try:
                self.deliver(recipient, msg_id, fields)
            except Exception as e:                      # e.g. sqlite error: report it, keep the worker alive
                self.log("Delivery of %s to %s crashed: %r" % (msg_id[:8], recipient, e))
                self._message_status(msg_id, recipient, "failed", reason=str(e))

    def _has_older_waiting(self, recipient, msg_id):
        with self.state_lock:
            pending = set(self.pending_acks)
        for other_id, _ in self.db.outbox_for(recipient):
            if other_id == msg_id:
                return False                        # reached ourselves: nothing older is waiting
            if (other_id, recipient) not in pending:
                return True
        return False

    def _expect_ack(self, msg_id, recipient):
        """Register the ACK we wait for BEFORE sending: on a fast link the ACK can arrive before
        send() returns, and an ACK for an unregistered message would later time out as 'failed'."""
        with self.state_lock:
            self.pending_acks[(msg_id, recipient)] = time.time() + config.ACK_TIMEOUT

    def _unexpect_ack(self, msg_id, recipient):
        with self.state_lock:
            self.pending_acks.pop((msg_id, recipient), None)

    def _send_queued(self, recipient, msg_id, fields):
        self._expect_ack(msg_id, recipient)
        try:
            self._send_frame(recipient, "MSG", dict(fields, id=msg_id))
        except HandshakeError as e:
            self._unexpect_ack(msg_id, recipient)
            if e.reason in ("version", "auth", "identity"):
                # Waiting will not help: the other side will never accept us.
                self.db.outbox_remove(msg_id, recipient)
                self._message_status(msg_id, recipient, "failed", reason=str(e))
                self._status("Could not send to %s: %s" % (recipient, e))
                return "failed"
            return self._queue_after_failure(recipient, msg_id, e)
        except (OSError, protocol.FrameError) as e:
            self._unexpect_ack(msg_id, recipient)
            return self._queue_after_failure(recipient, msg_id, e)
        self.log("Message sent to %s" % recipient)
        return "sent"

    def _queue_after_failure(self, recipient, msg_id, error):
        # The peer is probably gone: drop it from the list (as before). It comes back
        # with its next HELLO, and that is the moment flush_outbox() runs.
        if self.table.remove(recipient):
            self.on_peers_changed()
        self._message_status(msg_id, recipient, "queued")
        self._status("%s is not reachable (%s). The message is kept and sent when they are back."
                     % (recipient, explain_error(error)))
        return "queued"

    def flush_outbox(self, recipient):
        """Deliver everything queued for `recipient`, oldest first.

        Only one thread flushes a peer at a time (that keeps the order). A thread that
        finds the lock taken leaves a note ("flush again") and goes away; the running
        thread reads the note and makes another pass, so a message queued a moment
        ago is never forgotten. Note and lock are handled under ONE lock, otherwise a
        note could be left right after the running thread decided to stop."""
        with self.state_lock:
            lock = self.flush_locks.setdefault(recipient, threading.Lock())
            self.flush_again.add(recipient)
            if not lock.acquire(blocking=False):
                return
        while True:
            with self.state_lock:
                if recipient not in self.flush_again or self.stopping.is_set():
                    lock.release()
                    return
                self.flush_again.discard(recipient)
            try:
                self._flush_pass(recipient)
            except Exception as e:                  # never leave the lock taken (the queue would be stuck)
                self.log("Flushing the queue for %s crashed: %r" % (recipient, e))
                with self.state_lock:
                    lock.release()
                return

    def _flush_pass(self, recipient):
        """One pass over the queue of `recipient`. Stops at the first failure."""
        for msg_id, fields in self.db.outbox_for(recipient):
            if self.stopping.is_set():
                return
            with self.state_lock:
                if (msg_id, recipient) in self.pending_acks:
                    continue                        # already sent, only the ACK is missing
                self.pending_acks[(msg_id, recipient)] = time.time() + config.ACK_TIMEOUT
            self._message_status(msg_id, recipient, "sending")
            try:
                self._send_frame(recipient, "MSG", dict(fields, id=msg_id))
            except HandshakeError as e:
                self._unexpect_ack(msg_id, recipient)
                self.log("Delivery of queued message to %s failed: %s" % (recipient, e))
                if e.reason in ("version", "auth", "identity"):
                    self.db.outbox_remove(msg_id, recipient)
                    self._message_status(msg_id, recipient, "failed", reason=str(e))
                else:
                    self._message_status(msg_id, recipient, "queued")
                return
            except (OSError, protocol.FrameError) as e:
                self._unexpect_ack(msg_id, recipient)
                self._message_status(msg_id, recipient, "queued")
                self.log("Delivery of queued message to %s failed: %s" % (recipient, e))
                return
            self.log("Queued message delivered to %s" % recipient)

    def _message_status(self, msg_id, recipient, status, reason=""):
        self._event("msg_status", id=msg_id, peer=recipient, status=status, reason=reason)

    def send_message(self, name, text):
        """Simple API: send `text` to the peer called `name`. Returns True if it was sent now
        (False = too long, empty, or the peer was unreachable and the message was queued)."""
        text = text.strip()
        if text == "":
            return False
        if len(text) > config.MAX_TEXT_LEN:
            self.on_status("Message too long (max %d characters)." % config.MAX_TEXT_LEN)
            return False
        fields = {"text": text, "ts": time.strftime("%H:%M:%S")}
        return self.deliver(name, uuid.uuid4().hex, fields) == "sent"

    def send_typing(self, name):
        """Tell `name` that we are typing. At most one frame per TYPING_THROTTLE seconds."""
        now = time.time()
        with self.state_lock:
            if now - self.last_typing.get(name, 0) < config.TYPING_THROTTLE:
                return
            self.last_typing[name] = now

        def work():
            try:
                self._send_frame(name, "TYPING")
            except OSError:
                pass                                # typing hints are not worth an error message
        self._spawn(work)

    # ------------------------------------------------------------------
    # background work: ACK time-outs, PING / RTT, retrying the queue
    # ------------------------------------------------------------------
    def _housekeeping_loop(self):
        # First PING round 3 seconds after start (discovery needs a moment to find peers).
        last_round = time.time() - config.PING_INTERVAL + 3
        while not self.stopping.wait(1.0):
            self._expire_acks()
            if time.time() - last_round >= config.PING_INTERVAL:
                last_round = time.time()
                self._spawn(self._ping_and_retry)

    def _expire_acks(self):
        now = time.time()
        with self.state_lock:
            late = [key for key, deadline in self.pending_acks.items() if deadline < now]
            for key in late:
                del self.pending_acks[key]
        for msg_id, recipient in late:
            self.db.outbox_remove(msg_id, recipient)
            self.log("No ACK from %s for message %s" % (recipient, msg_id[:8]))
            self._message_status(msg_id, recipient, "failed", reason="no acknowledgement")

    def _ping_and_retry(self):
        self._retry_manual()
        for name in self.table.names():
            if self.stopping.is_set():
                return
            try:
                # Connect FIRST, then start the clock: the round trip must not include the time
                # it takes to open the TCP connection and do the key exchange.
                conn = self._get_connection(name, self.table.get(name))
                conn.send("PING", {"id": uuid.uuid4().hex[:8], "sent": time.perf_counter()})
            except (OSError, protocol.FrameError):
                self._drop_connection(name)
                continue
            if self.db.outbox_count(name):
                self.flush_outbox(name)             # a safety net in case a HELLO was missed

    def peer_stats(self):
        """For the Network info panel: one dict per online peer."""
        with self.state_lock:
            rtts = dict(self.rtt)
        stats = []
        with self.state_lock:
            connected = dict(self.connected_info)
        for name, info in sorted(self.table.snapshot().items()):
            encrypted, fingerprint = connected.get(name, (False, ""))
            stats.append({"name": name, "ip": info["ip"], "port": info["port"], "last_seen": info["last_seen"],
                          "rtt": rtts.get(name, (None, 0))[0], "encrypted": encrypted, "fingerprint": fingerprint})
        return stats
