"""
connection.py - one TCP connection to one other peer, in both directions.

A Connection knows how to
  * send ONE frame at a time (a lock makes sure two threads never mix their bytes),
  * read ONE frame (looping over partial reads, and checking the stop flag often),
  * optionally encrypt/decrypt each frame (the `secure` object, see crypto.py).

Why select() before recv()? We want a LONG timeout for sending (a big file chunk
to a slow peer may take seconds) but a SHORT wake-up for reading (so the reader
thread can notice that the program is quitting). A socket has only one timeout,
so the socket gets the long one and select() provides the short polling wake-up.
"""

import io
import select
import socket
import threading
import time

from . import config
from . import protocol


class Connection:
    def __init__(self, sock, address, outgoing):
        self.sock = sock
        self.address = address
        self.outgoing = outgoing           # True = we dialed it, False = we accepted it
        self.remote_name = None            # filled in by the HI handshake
        self.encrypted = False
        self.fingerprint = ""              # short hash of the peer's identity key (if encrypted)
        self.secure = None                 # a crypto.SecureChannel after a successful handshake
        self.closed = False
        self.send_lock = threading.Lock()
        self.thread = None
        sock.settimeout(config.SEND_TIMEOUT)

    # ------------------------------------------------------------------
    def send(self, frame_type, fields=None, data=b""):
        """Send one frame. Raises OSError if the connection is (or becomes) unusable."""
        raw = protocol.encode_frame(frame_type, fields, data)
        with self.send_lock:
            if self.closed:
                raise ConnectionError("connection is closed")
            payload = self.secure.seal(raw) if self.secure else raw
            try:
                self.sock.sendall(payload)
            except OSError:
                # A half-sent frame would corrupt the stream, so after ANY error the
                # connection is finished.
                self.close()
                raise

    def read_frame(self, stopping, deadline=None):
        """Wait for the next frame. Raises ConnectionError if the connection closes or the
        program is stopping, socket.timeout after `deadline`, FrameError for a bad frame."""
        def recv(count):
            return self._recv_some(count, stopping, deadline)

        if self.secure is None:
            return protocol.read_frame(recv)
        # Encrypted: [4-byte record length][AES-GCM ciphertext] wraps one whole plain frame.
        (length,) = protocol.LENGTH_PREFIX.unpack(protocol.read_exactly(recv, protocol.LENGTH_PREFIX.size))
        if length > protocol.MAX_HEADER_BYTES + protocol.MAX_CHUNK_BYTES + 1024:
            raise protocol.FrameError("encrypted record of %d bytes is not allowed" % length)
        plain = self.secure.open(protocol.read_exactly(recv, length))
        return protocol.read_frame(io.BytesIO(plain).read)

    def _recv_some(self, count, stopping, deadline):
        """recv() that wakes up every second to check `stopping`. Returns b"" when closed."""
        while True:
            if stopping.is_set() or self.closed:
                raise ConnectionError("stopping")
            if deadline is not None and time.time() > deadline:
                raise socket.timeout("timed out")
            try:
                readable, _, _ = select.select([self.sock], [], [], config.SOCKET_TIMEOUT)
                if not readable:
                    continue
                return self.sock.recv(count)
            except (OSError, ValueError):       # ValueError: select() on an already closed socket
                raise ConnectionError("socket error")

    def close(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)    # wakes up a thread blocked on this socket
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
