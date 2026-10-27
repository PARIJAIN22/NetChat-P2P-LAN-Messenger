import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netchat import protocol


class ProtocolTests(unittest.TestCase):
    def test_round_trip(self):
        line = protocol.encode("MSG", "Rahul", "hello there", "10:30:00")
        self.assertEqual(line, "MSG|Rahul|10:30:00|hello there\n")
        self.assertEqual(protocol.decode(line), ("MSG", "Rahul", "10:30:00", "hello there"))

    def test_pipe_and_newline_cannot_break_format(self):
        nasty = "a|b\nc\r\nd\\e\\n|||"
        line = protocol.encode("MSG", "Ra|hul", nasty)
        # exactly one newline (the terminator) and exactly 3 separators
        self.assertEqual(line.count("\n"), 1)
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(line.count("|"), 3)
        msg_type, sender, _ts, payload = protocol.decode(line)
        self.assertEqual(sender, "Ra|hul")
        self.assertEqual(payload, nasty)

    def test_unicode(self):
        text = "नमस्ते 😀 héllo"
        line = protocol.encode("MSG", "A", text)
        self.assertEqual(protocol.decode(line.encode("utf-8").decode("utf-8"))[3], text)

    def test_hello_payload(self):
        line = protocol.encode("HELLO", "Asha", "192.168.1.7:5000")
        self.assertEqual(protocol.decode(line)[3], "192.168.1.7:5000")

    def test_rejects_malformed(self):
        for bad in ["", "garbage", "MSG|only|three", "MSG|a|b|c|d",
                    "FOO|a|10:00:00|x", "MSG||10:00:00|x", "MSG|a|10:00:00|bad\\"]:
            with self.assertRaises(protocol.ProtocolError, msg=repr(bad)):
                protocol.decode(bad)

    def test_encode_rejects_unknown_type(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode("NOPE", "a", "b")

    def test_all_types(self):
        for t in protocol.MSG_TYPES:
            self.assertEqual(protocol.decode(protocol.encode(t, "x", "y"))[0], t)


import io
import json
import struct


def raw_frame(header, data=b""):
    """Build a frame by hand (bypassing encode_frame) so tests can make BAD ones."""
    body = json.dumps(header).encode("utf-8") if not isinstance(header, bytes) else header
    return struct.pack(">I", len(body)) + body + data


def one_byte_at_a_time(data):
    """A recv() that returns a single byte per call: the worst case of a fragmented stream."""
    stream = io.BytesIO(data)
    return lambda k: stream.read(1)


def read_all(data):
    return protocol.read_frame(io.BytesIO(data).read)


# Example fields for every frame type, so each one gets its own round-trip test.
SAMPLE_FIELDS = {
    "HI": {"name": "Rahul", "pass": False},
    "AUTH": {"proof": "ab12"},
    "MSG": {"id": "1", "text": "héllo | 你好 \U0001F600\nline2", "ts": "10:30:00",
            "group": {"id": "g1", "name": "Team", "members": ["A", "B"]}},
    "ACK": {"id": "1"},
    "TYPING": {},
    "PING": {"id": "p", "sent": 1.5},
    "PONG": {"id": "p", "sent": 1.5},
    "FILE_OFFER": {"fid": "f", "name": "a.txt", "size": 10, "sha256": "00" * 32},
    "FILE_ACCEPT": {"fid": "f"},
    "FILE_DECLINE": {"fid": "f"},
    "FILE_CHUNK": {"fid": "f", "seq": 0},
    "FILE_DONE": {"fid": "f"},
    "FILE_RESULT": {"fid": "f", "ok": False, "reason": "bad hash"},
    "FILE_CANCEL": {"fid": "f"},
    "ERROR": {"code": "version", "text": "no"},
}


class FrameTests(unittest.TestCase):
    def test_every_frame_type_is_documented_and_has_a_sample(self):
        self.assertEqual(set(SAMPLE_FIELDS), set(protocol.FRAME_TYPES))
        for name, doc in protocol.FRAME_TYPES.items():
            self.assertGreater(len(doc), 10, name)

    def test_round_trip_of_every_frame_type(self):
        for frame_type, fields in SAMPLE_FIELDS.items():
            data = bytes(range(256)) * 4 if frame_type == "FILE_CHUNK" else b""
            frame = read_all(protocol.encode_frame(frame_type, fields, data))
            self.assertEqual(frame.type, frame_type)
            self.assertEqual(frame.fields, fields, frame_type)
            self.assertEqual(frame.data, data, frame_type)

    def test_wire_layout(self):
        raw = protocol.encode_frame("ACK", {"id": "x"})
        (length,) = struct.unpack(">I", raw[:4])
        self.assertEqual(length, len(raw) - 4)
        self.assertEqual(json.loads(raw[4:]), {"v": 1, "t": "ACK", "id": "x"})

    def test_partial_reads(self):
        # a frame that arrives one byte at a time must still be decoded correctly
        chunk = bytes(range(256)) * 8
        raw = protocol.encode_frame("FILE_CHUNK", {"fid": "f", "seq": 3}, chunk)
        frame = protocol.read_frame(one_byte_at_a_time(raw))
        self.assertEqual((frame.type, frame.get("seq"), frame.data), ("FILE_CHUNK", 3, chunk))

    def test_two_frames_glued_together(self):
        stream = io.BytesIO(protocol.encode_frame("ACK", {"id": "1"}) + protocol.encode_frame("ACK", {"id": "2"}))
        self.assertEqual(protocol.read_frame(stream.read).get("id"), "1")
        self.assertEqual(protocol.read_frame(stream.read).get("id"), "2")

    def test_truncated_stream(self):
        raw = protocol.encode_frame("MSG", SAMPLE_FIELDS["MSG"])
        for cut in (0, 2, 4, len(raw) - 1):
            with self.assertRaises(ConnectionError, msg="cut at %d" % cut):
                read_all(raw[:cut])

    def test_oversized_header_is_rejected_before_reading_it(self):
        # announce a 2 GB header but send nothing: we must refuse without trying to read it
        with self.assertRaises(protocol.FrameError):
            read_all(struct.pack(">I", 2 * 1024 ** 3))
        with self.assertRaises(protocol.FrameError):
            read_all(struct.pack(">I", protocol.MAX_HEADER_BYTES + 1))
        with self.assertRaises(protocol.FrameError):
            read_all(struct.pack(">I", 0))

    def test_header_exactly_at_the_limit_is_still_accepted_by_the_length_check(self):
        # (it then fails as bad JSON, not as "too big": the limit is inclusive)
        with self.assertRaises(protocol.FrameError) as ctx:
            read_all(struct.pack(">I", protocol.MAX_HEADER_BYTES) + b"x" * protocol.MAX_HEADER_BYTES)
        self.assertIn("JSON", str(ctx.exception))

    def test_oversized_chunk_is_rejected(self):
        with self.assertRaises(protocol.FrameError):
            read_all(raw_frame({"v": 1, "t": "FILE_CHUNK", "fid": "f", "n": protocol.MAX_CHUNK_BYTES + 1}))
        with self.assertRaises(protocol.FrameError):
            protocol.encode_frame("FILE_CHUNK", {"fid": "f"}, b"x" * (protocol.MAX_CHUNK_BYTES + 1))

    def test_raw_data_only_allowed_for_file_chunks(self):
        with self.assertRaises(protocol.FrameError):
            protocol.encode_frame("MSG", {"id": "1"}, b"data")
        with self.assertRaises(protocol.FrameError):
            read_all(raw_frame({"v": 1, "t": "MSG", "n": 4}, b"data"))

    def test_bad_json(self):
        for bad in (b"not json at all", b"{broken", b"\xff\xfe\xfd", b"[1,2,3]", b'"text"', b"123"):
            with self.assertRaises(protocol.FrameError, msg=repr(bad)):
                read_all(raw_frame(bad))

    def test_wrong_version_is_rejected_cleanly(self):
        for version in (0, 2, 99, "1", None):
            with self.assertRaises(protocol.VersionError, msg=repr(version)):
                read_all(raw_frame({"v": version, "t": "MSG", "id": "1"}))
        with self.assertRaises(protocol.VersionError):
            read_all(raw_frame({"t": "MSG"}))                       # no version at all
        # a future version may use frame types we do not know: still reported as a VERSION problem
        with self.assertRaises(protocol.VersionError):
            read_all(raw_frame({"v": 2, "t": "BRAND_NEW_THING"}))

    def test_unknown_type_and_bad_length_field(self):
        with self.assertRaises(protocol.FrameError):
            read_all(raw_frame({"v": 1, "t": "NOPE"}))
        with self.assertRaises(protocol.FrameError):
            read_all(raw_frame({"v": 1}))
        for bad_n in (-1, "4", 1.5, True):
            with self.assertRaises(protocol.FrameError, msg=repr(bad_n)):
                read_all(raw_frame({"v": 1, "t": "FILE_CHUNK", "fid": "f", "n": bad_n}))

    def test_reserved_field_names_cannot_be_overridden(self):
        for name in ("v", "t", "n"):
            with self.assertRaises(protocol.FrameError):
                protocol.encode_frame("ACK", {name: 5})

    def test_encode_rejects_unknown_type(self):
        with self.assertRaises(protocol.FrameError):
            protocol.encode_frame("NOPE")


if __name__ == "__main__":
    unittest.main()
