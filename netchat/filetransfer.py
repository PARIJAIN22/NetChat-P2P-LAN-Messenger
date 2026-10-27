"""
filetransfer.py - sending and receiving files over a peer connection.

The conversation (frames are described in protocol.py):

    sender                               receiver
      |--- FILE_OFFER (name,size,sha256) -->|   the receiver's user sees Accept / Decline
      |<-- FILE_ACCEPT  (or FILE_DECLINE) --|
      |--- FILE_CHUNK x N (64 KB each) ---->|   written to disk as they arrive
      |--- FILE_DONE ---------------------->|   receiver checks size and SHA-256
      |<-- FILE_RESULT (ok / not ok) -------|
      either side can send FILE_CANCEL at any time

Resume (both sides new; an older peer simply ignores the extra fields and gets the old behaviour)
  * FILE_OFFER carries resume=true; every FILE_CHUNK carries off (byte offset) and h (SHA-256 of
    the chunk), so a damaged chunk is detected at once and asked for again (FILE_ACCEPT with offset).
  * When the connection breaks, both sides keep their state (also in the database, so it survives
    a restart) and the partial file. The sender offers the same fid again when the peer is back;
    the receiver answers FILE_ACCEPT {offset, prefix = SHA-256 of what it has}. The sender
    continues at offset only if its own first `offset` bytes have that hash, otherwise from 0.

Design notes
  * The file travels over the SAME TCP connection as the chat. Every frame is sent
    under the connection's send lock, so a chat message simply slips in between two
    chunks: chat never waits for a file. The sender runs in its own thread, the
    receiver handles chunks in the connection's reader thread.
  * Memory: the file is read, hashed and written in small pieces, never loaded as a
    whole. A 500 MB file needs the same few hundred KB of memory as a 5 KB file.
  * Flow control: TCP's window plus the chunk size. If the receiver's disk is slow, its
    socket buffer fills, TCP tells the sender to wait, sendall() blocks. No extra code.
  * Integrity: TCP already detects bit errors, but the SHA-256 over the whole file
    also catches a truncated or mixed-up transfer and proves what was saved.
"""

import hashlib
import os
import re
import shutil
import threading
import time
import uuid

from . import config
from . import protocol

MAX_FILE_BYTES = 100 * 1024 ** 3         # refuse offers bigger than this (sanity limit)
HASH_BLOCK = 1024 * 1024
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}


def sanitize_filename(name):
    """Make a received file name safe: no folders (path traversal!), no characters Windows rejects."""
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = "file"
    if name.split(".")[0].upper() in RESERVED_NAMES:
        name = "_" + name
    return name[:150]


def unique_path(folder, name):
    """folder/name, or folder/name (1).ext if that exists already. Never overwrites."""
    stem, ext = os.path.splitext(name)
    path = os.path.join(folder, name)
    counter = 1
    while os.path.exists(path):
        path = os.path.join(folder, "%s (%d)%s" % (stem, counter, ext))
        counter += 1
    return path


def file_sha256(path, should_stop=lambda: False):
    """SHA-256 of a file, read in 1 MB pieces. Returns None if should_stop() became true."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(HASH_BLOCK)
            if not block:
                return digest.hexdigest()
            if should_stop():
                return None
            digest.update(block)


class Transfer:
    """State of one transfer (either direction)."""

    def __init__(self, fid, peer, name, size, direction):
        self.fid = fid
        self.peer = peer
        self.name = name
        self.size = size
        self.direction = direction            # 'out' or 'in'
        self.sha256 = ""
        self.state = "new"                    # see FileManager._set_state
        self.done = 0                         # bytes sent / received so far
        self.conn = None                      # the Connection this transfer is bound to
        self.path = None                      # source file (out) or saved file (in)
        self.handle = None                    # open file (in)
        self.hasher = None                    # running SHA-256 (in)
        self.next_seq = 0
        self.cancel = threading.Event()
        # The GUI thread (accept / cancel) and the reader thread (chunks) both touch `state` and
        # `handle`. Without this lock a Cancel could close the file while a chunk is being written.
        # Re-entrant, because helpers like _discard_partial() are called with it already held.
        self.lock = threading.RLock()
        self.started = 0.0
        self.last_report = 0.0
        self.last_bytes = 0
        # ---- resume (all guarded by `lock`) ----
        self.resumable = False                # in: the sender can resume. out: the receiver said it can
        self.owns_file = False                # in: we created transfer.path (so we may delete it)
        self.awaiting_resend = False          # in: a damaged chunk was re-requested, skip until it arrives
        self.bad_chunks = 0                   # in: damaged chunks so far (bounded by MAX_CHUNK_RETRIES)
        self.resume_busy = False              # in: a thread is preparing a resume
        self.loop_running = False             # out: a _send_loop thread is active
        self.restart = None                   # out: (offset, prefix) requested while the loop runs
        self.resuming = False                 # out: a _resume_worker thread is active
        self.last_persist = 0.0


class FileManager:
    def __init__(self, peer):
        self.peer = peer
        self.lock = threading.Lock()
        self.transfers = {}                   # fid -> Transfer
        self.chunk_delay = 0.0                # seconds between chunks: lets tests slow a transfer down

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _event(self, kind, transfer, **extra):
        self.peer._event(kind, fid=transfer.fid, peer=transfer.peer, direction=transfer.direction,
                         name=transfer.name, size=transfer.size, **extra)

    def _set_state(self, transfer, state, reason="", **extra):
        """Move a transfer to a new state and tell the GUI.

        States:  hashing -> offered -> sending -> verifying -> verified     (sender)
                 offered -> receiving -> verified                           (receiver)
                 sending / receiving / offered / verifying -> interrupted -> (resume) sending / receiving
                 any -> failed | declined | cancelled
        """
        transfer.state = state
        self.peer.log("File %s (%s): %s%s" % (transfer.name, transfer.fid[:8], state, " - " + reason if reason else ""))
        if self._finished(transfer):
            self._db(self.peer.db.delete_transfer, transfer.fid)        # nothing left to resume
        self._event("file_state", transfer, state=state, reason=reason, path=transfer.path, **extra)

    def _finished(self, transfer):
        return transfer.state in ("verified", "failed", "declined", "cancelled")

    def _db(self, function, *args):
        """Persisting progress is best effort: a database problem must not break a transfer."""
        try:
            function(*args)
        except Exception as e:
            self.peer.log("Could not save the transfer state: %r" % e)

    def _persist(self, transfer):
        transfer.last_persist = time.time()
        self._db(lambda: self.peer.db.save_transfer(
            fid=transfer.fid, direction=transfer.direction, peer=transfer.peer, name=transfer.name,
            size=transfer.size, sha256=transfer.sha256, path=transfer.path or "", done=transfer.done))

    def _maybe_persist(self, transfer):
        if time.time() - transfer.last_persist >= config.PERSIST_INTERVAL:
            transfer.last_persist = time.time()
            self._db(self.peer.db.update_transfer_done, transfer.fid, transfer.done)

    def _progress(self, transfer, force=False):
        now = time.time()
        if not force and now - transfer.last_report < config.PROGRESS_INTERVAL:
            return
        interval = max(now - transfer.last_report, 1e-6)
        speed = (transfer.done - transfer.last_bytes) / interval if transfer.last_report else 0.0
        transfer.last_report, transfer.last_bytes = now, transfer.done
        self._event("file_progress", transfer, done=transfer.done, speed=speed)

    def _send_control(self, transfer, frame_type, fields=None):
        try:
            transfer.conn.send(frame_type, dict(fid=transfer.fid, **(fields or {})))
            return True
        except (OSError, protocol.FrameError, AttributeError):    # AttributeError: no connection (None)
            return False

    def _tell_later(self, transfer, frame_type):
        """Best effort, in a thread: tell the peer about a decision although our connection is gone."""
        def work():
            try:
                self.peer._send_frame(transfer.peer, frame_type, {"fid": transfer.fid})
            except (OSError, protocol.FrameError) as e:
                self.peer.log("Could not tell %s about %s: %s" % (transfer.peer, transfer.fid[:8], e))
        self.peer._spawn(work)

    @staticmethod
    def _prefix_sha256(path, length):
        """SHA-256 object of the first `length` bytes of a file."""
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            while length > 0:
                block = f.read(min(HASH_BLOCK, length))
                if not block:
                    break
                digest.update(block)
                length -= len(block)
        return digest

    def _can_resume(self, transfer):
        if transfer.direction == "in":
            return transfer.resumable and transfer.state == "receiving"
        return transfer.state == "offered" or (transfer.resumable and transfer.state in ("sending", "verifying"))

    def _interrupt(self, transfer, reason):
        """Called with transfer.lock held: keep everything needed to continue later."""
        if transfer.handle is not None:
            try:
                transfer.handle.close()
            except OSError as e:
                self.peer.log("Could not close %s: %s" % (transfer.path, e))
            transfer.handle = None
        transfer.awaiting_resend = False
        self._set_state(transfer, "interrupted", reason)
        self._persist(transfer)

    # ------------------------------------------------------------------
    # start / stop / connections
    # ------------------------------------------------------------------
    def load(self):
        """At start-up: bring back the transfers that were interrupted last time."""
        try:
            rows = self.peer.db.saved_transfers()
        except Exception as e:
            self.peer.log("Could not load saved transfers: %r" % e)
            return
        for row in rows:
            transfer = Transfer(row["fid"], row["peer"], row["name"], row["size"], row["direction"])
            transfer.sha256, transfer.path, transfer.done = row["sha256"], row["path"], row["done"]
            transfer.state, transfer.resumable, transfer.owns_file = "interrupted", True, row["direction"] == "in"
            with self.lock:
                if transfer.fid in self.transfers:
                    continue
                self.transfers[transfer.fid] = transfer
            if transfer.direction == "out" and not (os.path.isfile(transfer.path)
                                                    and os.path.getsize(transfer.path) == transfer.size):
                with transfer.lock:
                    self._set_state(transfer, "failed", "the file was moved or changed, it cannot be resumed")
                continue
            self.peer.log("File %s (%s): interrupted last time, will resume" % (transfer.name, transfer.fid[:8]))

    def stop(self):
        """The program is closing. Resumable transfers keep their partial file and saved state."""
        with self.lock:
            transfers = list(self.transfers.values())
        for transfer in transfers:
            with transfer.lock:
                if self._finished(transfer):
                    continue
                if transfer.state == "interrupted" or self._can_resume(transfer):
                    if transfer.handle is not None:
                        try:
                            transfer.handle.close()
                        except OSError as e:
                            self.peer.log("Could not close %s: %s" % (transfer.path, e))
                        transfer.handle = None
                    if transfer.state != "offered" or transfer.direction == "out":
                        self._persist(transfer)
                    continue
                transfer.cancel.set()
                self._discard_partial(transfer)

    def connection_lost(self, conn):
        """A connection died: resumable transfers on it are interrupted (and resumed later), the others fail."""
        with self.lock:
            lost = [t for t in self.transfers.values() if t.conn is conn and not self._finished(t)]
        for transfer in lost:
            with transfer.lock:
                if self._finished(transfer) or transfer.state == "interrupted" or transfer.conn is not conn:
                    continue
                if transfer.direction == "in" and transfer.state == "offered":
                    continue                    # not accepted yet: the sender offers it again when it is back
                if self._can_resume(transfer):
                    self._interrupt(transfer, "connection lost, resumes automatically")
                    resume = transfer.direction == "out"
                else:
                    resume = False
                    transfer.cancel.set()
                    self._discard_partial(transfer)
                    self._set_state(transfer, "failed", "connection lost")
            if resume:
                self.schedule_resume(transfer)

    def peer_online(self, name):
        """`name` is reachable again (new connection or discovery): resume what we were sending to it."""
        with self.lock:
            waiting = [t for t in self.transfers.values()
                       if t.direction == "out" and t.peer == name and t.state == "interrupted"]
        for transfer in waiting:
            self.schedule_resume(transfer)

    def _discard_partial(self, transfer):
        """Close and delete the half-written file of an unfinished download."""
        with transfer.lock:
            if transfer.direction != "in":
                return
            if transfer.handle is not None:
                try:
                    transfer.handle.close()
                except OSError as e:
                    self.peer.log("Could not close %s: %s" % (transfer.path, e))
                transfer.handle = None
            if transfer.owns_file and transfer.state != "verified" and transfer.path and os.path.exists(transfer.path):
                try:
                    os.remove(transfer.path)
                except OSError as e:
                    self.peer.log("Could not delete the partial file %s: %s" % (transfer.path, e))

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    def offer(self, peer_name, path, fid=None):
        """Start sending `path` to `peer_name`. Returns the transfer id immediately; progress is
        reported through 'file_state' / 'file_progress' events."""
        fid = fid or uuid.uuid4().hex
        transfer = Transfer(fid, peer_name, os.path.basename(path), os.path.getsize(path), "out")
        transfer.path = path
        with self.lock:
            self.transfers[fid] = transfer
        self._set_state(transfer, "hashing")
        self.peer._spawn(self._offer_worker, transfer)
        return fid

    @staticmethod
    def _offer_fields(transfer):
        # "resume": this sender understands FILE_ACCEPT offsets and adds per-chunk hashes.
        return {"fid": transfer.fid, "name": transfer.name, "size": transfer.size, "sha256": transfer.sha256,
                "resume": True}

    def _offer_worker(self, transfer):
        try:
            transfer.sha256 = file_sha256(transfer.path, transfer.cancel.is_set)
        except OSError as e:
            self._set_state(transfer, "failed", "cannot read the file: %s" % e)
            return
        if transfer.sha256 is None:
            return                                           # cancelled while hashing
        try:
            conn = self.peer._send_frame(transfer.peer, "FILE_OFFER", self._offer_fields(transfer))
        except OSError as e:
            from .peer import explain_error
            self._set_state(transfer, "failed", "%s is not reachable (%s)" % (transfer.peer, explain_error(e)))
            return
        with transfer.lock:
            if transfer.state == "hashing" and not transfer.cancel.is_set():
                transfer.conn = conn
                self._set_state(transfer, "offered")
                self._persist(transfer)

    def schedule_resume(self, transfer):
        with transfer.lock:
            if transfer.resuming or transfer.state != "interrupted":
                return
            transfer.resuming = True
        self.peer._spawn(self._resume_worker, transfer)

    def _resume_worker(self, transfer):
        """Offer an interrupted transfer again, with bounded retries and growing pauses. If the peer is
        gone, stop: peer_online() starts a new worker when it is back."""
        try:
            for attempt, delay in enumerate(config.RESUME_DELAYS, 1):
                if self.peer.stopping.wait(delay):
                    return
                with transfer.lock:
                    if transfer.state != "interrupted":
                        return
                if self.peer.table.get(transfer.peer) is None:
                    return
                try:
                    conn = self.peer._send_frame(transfer.peer, "FILE_OFFER", self._offer_fields(transfer))
                except (OSError, protocol.FrameError) as e:
                    self.peer.log("Resume of %s, attempt %d failed: %s" % (transfer.name, attempt, e))
                    if getattr(e, "reason", "") in ("version", "auth", "identity"):
                        return                                   # waiting will not help
                    continue
                with transfer.lock:
                    if transfer.state == "interrupted":
                        transfer.conn = conn
                self.peer.log("File %s (%s): offered again to resume" % (transfer.name, transfer.fid[:8]))
                return
            self.peer.log("File %s: %s did not answer, will resume when it is back" % (transfer.name, transfer.peer))
        finally:
            with transfer.lock:
                transfer.resuming = False

    def _checked_offset(self, transfer, offset, prefix):
        """The receiver asks to continue at `offset` and sends the SHA-256 of what it has. Continue
        there only if our first `offset` bytes have the same hash; otherwise start from 0."""
        if not offset:
            return 0
        if (not isinstance(offset, int) or isinstance(offset, bool) or not 0 < offset <= transfer.size
                or offset % config.FILE_CHUNK_SIZE or not isinstance(prefix, str)):
            self.peer.log("File %s: ignored a bad resume offset %r" % (transfer.name, offset))
            return 0
        if self._prefix_sha256(transfer.path, offset).hexdigest() != prefix:
            self.peer.log("File %s: the receiver's partial file differs, starting again" % transfer.name)
            return 0
        return offset

    def _send_loop(self, transfer, offset=0, prefix=None):
        """Runs in its own thread after the receiver accepted: read, send, repeat."""
        interrupted = False
        try:
            offset = self._checked_offset(transfer, offset, prefix)
            with transfer.lock:
                if transfer.cancel.is_set() or transfer.state not in ("offered", "interrupted", "sending", "verifying"):
                    return
                transfer.done = offset
                transfer.started = time.time()
                transfer.last_report = 0.0
                self._set_state(transfer, "sending", "resuming at %d bytes" % offset if offset else "")
                self._persist(transfer)
            with open(transfer.path, "rb") as f:
                f.seek(offset)
                while True:
                    if transfer.cancel.is_set() or self.peer.stopping.is_set():
                        return
                    with transfer.lock:
                        if transfer.state != "sending":
                            return                           # interrupted / cancelled meanwhile
                        restart, transfer.restart = transfer.restart, None
                        conn = transfer.conn
                    if restart is not None:                  # the receiver re-requested from here
                        start = restart[0]
                        if (isinstance(start, int) and not isinstance(start, bool) and 0 <= start <= transfer.size
                                and start % config.FILE_CHUNK_SIZE == 0):
                            f.seek(start)
                            transfer.done = start
                    position = transfer.done
                    chunk = f.read(config.FILE_CHUNK_SIZE)
                    if not chunk:
                        self._progress(transfer, force=True)
                        conn.send("FILE_DONE", {"fid": transfer.fid})
                        with transfer.lock:
                            if transfer.restart is not None:
                                continue                     # a re-request arrived meanwhile
                            if transfer.state == "sending":
                                self._set_state(transfer, "verifying")   # waiting for the receiver's FILE_RESULT
                        return
                    # conn.send() takes the connection's send lock for ONE frame only, so chat
                    # messages from other threads get their turn between two chunks.
                    # "off" and "h" (SHA-256 of this chunk) let the receiver verify and resume.
                    conn.send("FILE_CHUNK", {"fid": transfer.fid, "seq": position // config.FILE_CHUNK_SIZE,
                                             "off": position, "h": hashlib.sha256(chunk).hexdigest()}, chunk)
                    transfer.done = position + len(chunk)
                    self._progress(transfer)
                    self._maybe_persist(transfer)
                    if self.chunk_delay:
                        time.sleep(self.chunk_delay)
        except (OSError, protocol.FrameError) as e:
            with transfer.lock:
                if not transfer.cancel.is_set() and not self._finished(transfer) and transfer.state != "interrupted":
                    if self._can_resume(transfer):
                        self._interrupt(transfer, "sending stopped (%s), resumes automatically" % e)
                        interrupted = True
                    else:
                        self._set_state(transfer, "failed", "sending stopped: %s" % e)
        finally:
            with transfer.lock:
                transfer.loop_running = False
                # A FILE_ACCEPT that arrived while this loop was ending must not be lost.
                again = transfer.restart if transfer.state in ("interrupted", "sending", "verifying") else None
                transfer.restart = None
                if again is not None and not interrupted:
                    transfer.loop_running = True
            if again is not None and not interrupted:
                self.peer._spawn(self._send_loop, transfer, *again)
        if interrupted:
            self.schedule_resume(transfer)

    # ------------------------------------------------------------------
    # receiving: the user's decisions (called from the GUI / CLI thread)
    # ------------------------------------------------------------------
    def _accept_fields(self, transfer):
        if not transfer.resumable:
            return {}                                        # an older sender: plain FILE_ACCEPT
        return {"resume": True, "offset": transfer.done, "prefix": transfer.hasher.copy().hexdigest()}

    def accept(self, fid):
        """The user pressed Accept. Returns the path the file will be saved to."""
        transfer = self._get(fid, "in")
        if transfer is None:
            return None
        with transfer.lock:
            if transfer.state != "offered":
                return None
            folder = self.peer.download_dir
            try:
                os.makedirs(folder, exist_ok=True)
                free = shutil.disk_usage(folder).free
                if transfer.size > free:
                    raise OSError("not enough free disk space (%d MB needed)" % (transfer.size // 1024 ** 2))
                transfer.path = unique_path(folder, transfer.name)
                transfer.handle = open(transfer.path, "wb")
                transfer.owns_file = True
            except OSError as e:
                self._set_state(transfer, "failed", str(e))
                error = e
            else:
                error = None
                transfer.hasher = hashlib.sha256()
                transfer.started = time.time()
                self._set_state(transfer, "receiving")
                if transfer.resumable:
                    self._persist(transfer)
            fields = self._accept_fields(transfer) if error is None else None
        # Network I/O outside the lock: a busy connection must not block the reader thread.
        if error is not None:
            self._send_control(transfer, "FILE_DECLINE")
            return None
        if not self._send_control(transfer, "FILE_ACCEPT", fields):
            with transfer.lock:
                if transfer.state == "receiving":
                    if transfer.resumable:           # accepted: it continues by itself when the sender is back
                        self._interrupt(transfer, "connection lost, resumes automatically")
                    else:
                        self._discard_partial(transfer)
                        self._set_state(transfer, "failed", "connection lost")
            return transfer.path if transfer.state == "interrupted" else None
        return transfer.path

    def decline(self, fid):
        transfer = self._get(fid, "in")
        if transfer is None:
            return
        with transfer.lock:
            if transfer.state != "offered":
                return
            self._set_state(transfer, "declined")
        if not self._send_control(transfer, "FILE_DECLINE"):
            self._tell_later(transfer, "FILE_DECLINE")

    def cancel(self, fid):
        """The user pressed Cancel on a transfer in either direction."""
        transfer = self._get(fid)
        if transfer is None:
            return
        with transfer.lock:
            if self._finished(transfer):
                return
            was_interrupted = transfer.state == "interrupted"
            transfer.cancel.set()
            self._discard_partial(transfer)
            self._set_state(transfer, "cancelled")
        if was_interrupted or not self._send_control(transfer, "FILE_CANCEL"):
            if transfer.conn is not None or was_interrupted:
                self._tell_later(transfer, "FILE_CANCEL")    # so the other side drops its saved state too

    def _get(self, fid, direction=None):
        with self.lock:
            transfer = self.transfers.get(fid)
        if transfer is not None and direction and transfer.direction != direction:
            return None
        return transfer

    # ------------------------------------------------------------------
    # frames from the network (called in the connection's reader thread)
    # ------------------------------------------------------------------
    def on_frame(self, conn, frame):
        handler = getattr(self, "_on_" + frame.type.lower(), None)
        if handler is not None:
            handler(conn, frame)

    def _transfer_for(self, conn, frame, direction):
        """The transfer a frame belongs to, only if it came from the right peer."""
        transfer = self._get(frame.get("fid"), direction)
        if transfer is None or transfer.peer != conn.remote_name:
            return None
        return transfer

    def _on_file_offer(self, conn, frame):
        fid, name, size, sha = frame.get("fid"), frame.get("name"), frame.get("size"), frame.get("sha256")
        valid = (isinstance(fid, str) and 0 < len(fid) <= 64 and isinstance(name, str) and name
                 and isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= MAX_FILE_BYTES
                 and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha))
        if not valid:
            self.peer.log("Ignored a malformed FILE_OFFER from %s" % conn.remote_name)
            return
        transfer = Transfer(fid, conn.remote_name, sanitize_filename(name), size, "in")
        transfer.sha256, transfer.conn = sha, conn
        transfer.state = "offered"
        transfer.resumable = frame.get("resume") is True
        with self.lock:                                      # check and insert in ONE step
            known = self.transfers.get(fid)
            if known is None:
                self.transfers[fid] = transfer
        if known is None:
            self.peer.log("File offered by %s: %s (%d bytes)" % (conn.remote_name, transfer.name, size))
            self._event("file_offer", transfer, sha256=sha)
            return
        self._offered_again(known, conn, size, sha)

    def _offered_again(self, transfer, conn, size, sha):
        """The sender offers a transfer we already know: it reconnected and wants to continue."""
        if transfer.direction != "in" or transfer.peer != conn.remote_name or (transfer.size, transfer.sha256) != (size, sha):
            return
        with transfer.lock:
            if transfer.state == "offered":
                transfer.conn = conn                         # still waiting for the user: answer on the new connection
                return
            if transfer.state == "receiving" and transfer.conn is not conn and transfer.resumable:
                self._interrupt(transfer, "the sender reconnected")
            if transfer.state == "verified":
                answer = True
            elif transfer.state == "interrupted" and not transfer.resume_busy:
                transfer.conn, transfer.resume_busy, answer = conn, True, False
            else:
                return                                       # the same offer again
        if answer:                                           # we have it already: the sender missed our result
            try:
                conn.send("FILE_RESULT", {"fid": transfer.fid, "ok": True})
            except (OSError, protocol.FrameError) as e:
                self.peer.log("Could not confirm %s: %s" % (transfer.name, e))
            return
        # Re-hashing the partial file can take a while: not in the connection's reader thread.
        self.peer._spawn(self._resume_incoming, transfer)

    def _resume_incoming(self, transfer):
        """Continue an interrupted download: keep the whole chunks we have, tell the sender where we are."""
        handle = None
        try:
            have = os.path.getsize(transfer.path) if transfer.path and os.path.exists(transfer.path) else 0
            offset = min(have, transfer.size) // config.FILE_CHUNK_SIZE * config.FILE_CHUNK_SIZE
            if not transfer.path:
                os.makedirs(self.peer.download_dir, exist_ok=True)
                transfer.path = unique_path(self.peer.download_dir, transfer.name)
            handle = open(transfer.path, "r+b" if have else "wb")
            handle.truncate(offset)
            handle.seek(offset)
            hasher = self._prefix_sha256(transfer.path, offset)
            with transfer.lock:
                if transfer.state != "interrupted":
                    handle.close()
                    return
                transfer.handle, handle = handle, None
                transfer.hasher, transfer.done = hasher, offset
                transfer.next_seq = offset // config.FILE_CHUNK_SIZE
                transfer.owns_file, transfer.awaiting_resend, transfer.bad_chunks = True, False, 0
                transfer.started, transfer.last_report = time.time(), 0.0
                self._set_state(transfer, "receiving", "resuming at %d bytes" % offset if offset else "")
                fields = self._accept_fields(transfer)
            if not self._send_control(transfer, "FILE_ACCEPT", fields):
                with transfer.lock:
                    if transfer.state == "receiving":
                        self._interrupt(transfer, "connection lost, resumes automatically")
        except OSError as e:
            if handle is not None:
                handle.close()
            with transfer.lock:
                if not self._finished(transfer):
                    self._discard_partial(transfer)
                    self._set_state(transfer, "failed", "cannot resume: %s" % e)
            self._send_control(transfer, "FILE_CANCEL")
        finally:
            with transfer.lock:
                transfer.resume_busy = False

    def _on_file_accept(self, conn, frame):
        transfer = self._transfer_for(conn, frame, "out")
        if transfer is None:
            return
        offset, prefix = frame.get("offset", 0), frame.get("prefix")
        with transfer.lock:
            if frame.get("resume") is True:
                transfer.resumable = True
            fresh = transfer.state == "interrupted" or (transfer.state == "offered" and transfer.conn is conn)
            again = transfer.state in ("sending", "verifying") and transfer.conn is conn   # damaged chunk re-requested
            if not (fresh or again):
                return
            transfer.conn = conn
            if transfer.loop_running:
                transfer.restart = (offset, prefix)
                return
            transfer.loop_running = True
        self.peer._spawn(self._send_loop, transfer, offset, prefix)

    def _on_file_decline(self, conn, frame):
        transfer = self._transfer_for(conn, frame, "out")
        if transfer is None:
            return
        with transfer.lock:
            if not self._finished(transfer):
                transfer.cancel.set()
                self._set_state(transfer, "declined")

    def _on_file_chunk(self, conn, frame):
        transfer = self._transfer_for(conn, frame, "in")
        if transfer is None or transfer.conn is not conn:
            return
        seq, data, offset, digest = frame.get("seq"), frame.data, frame.get("off"), frame.get("h")
        problem, rerequest = None, None
        with transfer.lock:
            if transfer.state != "receiving" or transfer.handle is None:
                return                                       # cancelled / interrupted meanwhile
            if offset is not None and transfer.awaiting_resend and offset != transfer.done:
                return                                       # chunks sent before the sender saw our re-request
            if offset == 0 and transfer.done > 0:            # the sender refused to resume: start from scratch
                transfer.handle.seek(0)
                transfer.handle.truncate()
                transfer.hasher, transfer.done, transfer.next_seq = hashlib.sha256(), 0, 0
            if ((offset is not None and offset != transfer.done) or seq != transfer.next_seq
                    or transfer.done + len(data) > transfer.size):
                problem = "unexpected data from the sender"
            elif digest is not None and hashlib.sha256(data).hexdigest() != digest:
                transfer.bad_chunks += 1
                if transfer.bad_chunks > config.MAX_CHUNK_RETRIES:
                    problem = "chunk %s arrived damaged %d times" % (seq, transfer.bad_chunks)
                else:
                    self.peer.log("File %s: chunk %s is damaged, asking for it again" % (transfer.name, seq))
                    transfer.awaiting_resend = True
                    rerequest = self._accept_fields(transfer)
            else:
                try:
                    transfer.handle.write(data)
                except OSError as e:
                    problem = "cannot write to disk: %s" % e
            if problem is None and rerequest is None:
                transfer.hasher.update(data)
                transfer.done += len(data)
                transfer.next_seq += 1
                transfer.awaiting_resend = False
            elif problem is not None:
                self._discard_partial(transfer)
                self._set_state(transfer, "failed", problem)
        if rerequest is not None:
            self._send_control(transfer, "FILE_ACCEPT", rerequest)
        elif problem is None:
            self._progress(transfer)
            if transfer.resumable:
                self._maybe_persist(transfer)
        else:
            self._send_control(transfer, "FILE_CANCEL")

    def _on_file_done(self, conn, frame):
        transfer = self._transfer_for(conn, frame, "in")
        if transfer is None:
            return
        with transfer.lock:
            if transfer.state != "receiving" or transfer.handle is None or transfer.conn is not conn:
                return
            if transfer.awaiting_resend:
                return                                       # the re-requested chunks (and a new DONE) follow
            try:
                transfer.handle.close()
            except OSError as e:
                self.peer.log("Could not close %s: %s" % (transfer.path, e))
            transfer.handle = None
            if transfer.done != transfer.size:
                reason = "received %d of %d bytes" % (transfer.done, transfer.size)
            elif transfer.hasher.hexdigest() != transfer.sha256:
                reason = "SHA-256 does not match: the file is damaged"
            else:
                reason = ""
            if reason:
                try:
                    os.remove(transfer.path)
                except OSError as e:
                    self.peer.log("Could not delete the damaged file %s: %s" % (transfer.path, e))
                self._set_state(transfer, "failed", reason)
            else:
                self._set_state(transfer, "verified")
        self._progress(transfer, force=True)
        self._send_control(transfer, "FILE_RESULT", {"ok": False, "reason": reason} if reason else {"ok": True})

    def _on_file_result(self, conn, frame):
        transfer = self._transfer_for(conn, frame, "out")
        if transfer is None:
            return
        with transfer.lock:
            if self._finished(transfer):
                return
            if frame.get("ok") is True:
                transfer.done = transfer.size
                self._set_state(transfer, "verified")
            else:
                self._set_state(transfer, "failed", "receiver reports: %s" % frame.get("reason", "?"))

    def _on_file_cancel(self, conn, frame):
        transfer = self._transfer_for(conn, frame, None)
        if transfer is None:
            return
        with transfer.lock:
            if self._finished(transfer):
                return
            transfer.cancel.set()
            self._discard_partial(transfer)
            self._set_state(transfer, "cancelled", "cancelled by %s" % conn.remote_name)
