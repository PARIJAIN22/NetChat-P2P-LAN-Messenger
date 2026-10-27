"""
protocol.py - the NetChat wire format.

One message = one line of UTF-8 text:

    TYPE|SENDER|TIMESTAMP|PAYLOAD\n

TYPE is one of HELLO, REPLY, MSG, BYE.

Problem: the user could type a "|" or press Enter inside a message, which
would break the format (extra fields / a message split in two). Solution: we
ESCAPE those characters before sending and UNESCAPE them on arrival:

    \\  ->  \\\\      (the escape character itself must be escaped first)
    |   ->  \\p
    \\n  ->  \\n   (backslash + letter n)
    \\r  ->  \\r

After escaping, a raw "|" or newline can only be a real separator.

THAT LINE FORMAT IS NOW USED ONLY FOR UDP DISCOVERY (HELLO / REPLY / BYE).
TCP uses length-prefixed JSON frames, described in the second half of this file,
because a text line cannot carry binary file data safely.
"""

import json
import struct
import time

MSG_TYPES = ("HELLO", "REPLY", "MSG", "BYE")


class ProtocolError(Exception):
    """Raised when a received line is not a valid NetChat message."""


def escape(text):
    # Order matters: escape the backslash FIRST, otherwise we would double-escape
    # the backslashes that the next lines add.
    text = text.replace("\\", "\\\\")
    text = text.replace("|", "\\p")
    text = text.replace("\n", "\\n")
    text = text.replace("\r", "\\r")
    return text


def unescape(text):
    # Walk through the text one character at a time; a backslash means
    # "the next character is a code".
    codes = {"\\": "\\", "p": "|", "n": "\n", "r": "\r"}
    result = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 >= len(text) or text[i + 1] not in codes:
                raise ProtocolError("bad escape sequence")
            result.append(codes[text[i + 1]])
            i += 2
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def encode(msg_type, sender, payload="", timestamp=None):
    """Build one protocol line (a str ending in newline). Caller turns it into bytes."""
    if msg_type not in MSG_TYPES:
        raise ProtocolError("unknown message type: %r" % msg_type)
    if timestamp is None:
        timestamp = time.strftime("%H:%M:%S")
    return "%s|%s|%s|%s\n" % (msg_type, escape(sender), timestamp, escape(payload))


def decode(line):
    """Parse one line (newline optional). Returns (msg_type, sender, timestamp, payload).

    Raises ProtocolError for anything malformed - callers just ignore such lines
    so a bad packet from the network can never crash the program.
    """
    line = line.rstrip("\r\n")
    parts = line.split("|")
    # Because user text has no raw "|" any more, a good line has exactly 4 parts.
    if len(parts) != 4:
        raise ProtocolError("expected 4 fields, got %d" % len(parts))
    msg_type, sender, timestamp, payload = parts
    if msg_type not in MSG_TYPES:
        raise ProtocolError("unknown message type: %r" % msg_type)
    sender = unescape(sender)
    if sender == "":
        raise ProtocolError("empty sender")
    return msg_type, sender, timestamp, unescape(payload)


# ======================================================================
# TCP frames: 4-byte length + JSON header (+ raw bytes for file chunks)
# ======================================================================
#
#   +----------------+--------------------------+----------------------+
#   | 4 bytes        | header_len bytes         | n bytes (optional)   |
#   | header_len     | UTF-8 JSON object        | raw file data        |
#   | big-endian     | {"v":1,"t":"MSG",...}    | only for FILE_CHUNK  |
#   +----------------+--------------------------+----------------------+
#
# Why a length prefix? TCP is a byte STREAM: it has no message boundaries, and
# file data may contain ANY byte (including newlines), so "read until \n" is
# impossible. With a length in front the receiver knows exactly how many bytes
# belong to this frame and keeps reading until they have all arrived.
#
# The JSON header always has
#     "v" - protocol version (integer). Another version is rejected cleanly.
#     "t" - frame type (one of FRAME_TYPES below)
# and, only when raw bytes follow the header,
#     "n" - number of raw bytes after the header (FILE_CHUNK only)
# plus the type-specific fields documented here.

PROTOCOL_VERSION = 1
LENGTH_PREFIX = struct.Struct(">I")          # 4 bytes, big-endian, unsigned
MAX_HEADER_BYTES = 1024 * 1024               # a JSON header may not be bigger than 1 MB
MAX_CHUNK_BYTES = 256 * 1024                 # raw bytes after a FILE_CHUNK header (we send 64 KB)

# Every frame type, with the meaning of its fields.
FRAME_TYPES = {
    "HI": "Handshake, the first frame in BOTH directions of a new connection. "
          "Fields: name (sender's username), pass (bool: a shared passphrase is set), "
          "and when encryption is available: ident (Ed25519 public key, hex), "
          "eph (X25519 public key, hex), sig (signature of eph, hex), "
          "proof (key-confirmation, only in the answering HI), port (optional: our TCP server port).",
    "AUTH": "Key confirmation from the dialing side after it verified the answering HI. "
            "Fields: proof (hex HMAC over both ephemeral keys). Sent in plaintext, "
            "everything after it is encrypted.",
    "MSG": "A chat message. Fields: id (uuid4, used by ACK and for de-duplication), "
           "text, ts (HH:MM:SS shown to the user), and optionally group "
           "{id, name, members} when the message belongs to a group chat.",
    "ACK": "Delivery receipt. Fields: id (of the MSG that arrived).",
    "TYPING": "'The sender is typing' (no fields). Sent at most once per 2 seconds.",
    "PING": "Round-trip-time probe. Fields: id, sent (sender's clock, echoed back).",
    "PONG": "Answer to a PING. Fields: id, sent (copied from the PING).",
    "FILE_OFFER": "'I want to send you a file'. Fields: fid (transfer id), name, size (bytes), "
                  "sha256 (hex digest of the whole file), resume (optional bool: sender can resume).",
    "FILE_ACCEPT": "Receiver agrees to the offer. Fields: fid; when resuming also resume (true), offset "
                   "(bytes the receiver has) and prefix (SHA-256 of them). The sender now starts the chunks.",
    "FILE_DECLINE": "Receiver refuses the offer. Fields: fid.",
    "FILE_CHUNK": "Up to 64 KB of file data. Fields: fid, seq (0,1,2...), n, and from resume-capable "
                  "senders off (byte offset) and h (SHA-256 of the chunk); the raw bytes follow the header.",
    "FILE_DONE": "Sender has sent every chunk. Fields: fid.",
    "FILE_RESULT": "Receiver's verdict after checking size and SHA-256. Fields: fid, ok (bool), "
                   "and when not ok: reason.",
    "FILE_CANCEL": "Either side stops the transfer. Fields: fid.",
    "ERROR": "Fatal problem; the connection is closed right after. Fields: code "
             "('version', 'auth', 'name', 'protocol'), text.",
}
FRAMES_WITH_DATA = ("FILE_CHUNK",)           # the only frames that may carry raw bytes


class FrameError(Exception):
    """A received frame is invalid (bad JSON, too big, unknown type, ...)."""


class VersionError(FrameError):
    """The other side speaks a different protocol version."""


class Frame:
    """One decoded frame: .type, .fields (dict) and .data (raw bytes, usually empty)."""

    def __init__(self, frame_type, fields=None, data=b""):
        self.type = frame_type
        self.fields = fields or {}
        self.data = data

    def get(self, key, default=None):
        return self.fields.get(key, default)

    def __repr__(self):
        return "Frame(%s, %r, %d data bytes)" % (self.type, self.fields, len(self.data))


def encode_frame(frame_type, fields=None, data=b""):
    """Build the bytes of one frame, ready for socket.sendall()."""
    if frame_type not in FRAME_TYPES:
        raise FrameError("unknown frame type: %r" % (frame_type,))
    fields = dict(fields or {})
    for reserved in ("v", "t", "n"):
        if reserved in fields:
            raise FrameError("field name %r is reserved" % reserved)
    header = {"v": PROTOCOL_VERSION, "t": frame_type}
    header.update(fields)
    if data:
        if frame_type not in FRAMES_WITH_DATA:
            raise FrameError("%s frames cannot carry raw data" % frame_type)
        if len(data) > MAX_CHUNK_BYTES:
            raise FrameError("chunk of %d bytes is too big" % len(data))
        header["n"] = len(data)
    raw = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_HEADER_BYTES:
        raise FrameError("header of %d bytes is too big" % len(raw))
    return LENGTH_PREFIX.pack(len(raw)) + raw + data


def read_exactly(recv, count):
    """Call recv(k) again and again until `count` bytes have arrived.

    One recv() on a TCP socket may return fewer bytes than asked for (a frame can
    arrive in several pieces), so a single call is never enough. recv returns b""
    when the other side closed the connection.
    """
    buffer = bytearray()
    while len(buffer) < count:
        piece = recv(count - len(buffer))
        if not piece:
            raise ConnectionError("connection closed in the middle of a frame")
        buffer += piece
    return bytes(buffer)


def read_frame(recv):
    """Read and validate ONE frame. `recv(k)` returns up to k bytes (b"" = closed).

    Every size is checked BEFORE the bytes are read, so a malicious peer cannot make
    us allocate a gigabyte by announcing one. Raises FrameError / VersionError for bad
    frames and ConnectionError when the stream ends early.
    """
    (header_len,) = LENGTH_PREFIX.unpack(read_exactly(recv, LENGTH_PREFIX.size))
    if header_len == 0 or header_len > MAX_HEADER_BYTES:
        raise FrameError("header length %d is not allowed (max %d)" % (header_len, MAX_HEADER_BYTES))
    raw = read_exactly(recv, header_len)
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FrameError("header is not valid UTF-8 JSON")
    if not isinstance(header, dict):
        raise FrameError("header must be a JSON object")
    if header.get("v") != PROTOCOL_VERSION:
        # Checked first: a newer version may have frame types we do not know.
        raise VersionError("protocol version %r is not supported (this program speaks %d)"
                           % (header.get("v"), PROTOCOL_VERSION))
    frame_type = header.pop("t", None)
    if frame_type not in FRAME_TYPES:
        raise FrameError("unknown frame type: %r" % (frame_type,))
    header.pop("v")
    size = header.pop("n", 0)
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise FrameError("bad data length field")
    if size and frame_type not in FRAMES_WITH_DATA:
        raise FrameError("%s frames cannot carry raw data" % frame_type)
    if size > MAX_CHUNK_BYTES:
        raise FrameError("data length %d is not allowed (max %d)" % (size, MAX_CHUNK_BYTES))
    data = read_exactly(recv, size) if size else b""
    return Frame(frame_type, header, data)
