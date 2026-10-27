"""
storage.py - everything NetChat keeps on disk, in one sqlite3 file (netchat.db).

  messages  the chat history (shown again when a chat is opened)
  outbox    messages that still wait for their ACK: the OFFLINE QUEUE
  groups    named group chats this user knows about
  peer_keys the identity key first seen for every peer name ("trust on first use")
  transfers unfinished file transfers, so they can resume after a disconnection or a restart

sqlite3 is in the standard library and survives crashes, so the queue is safe
even if the program is killed. Several threads use the same Database object (the
GUI thread writes history, network threads read/write the outbox), so every
operation holds one lock.

Every row has an `owner` (my username). Two programs that accidentally share one
file (two instances in the same folder) therefore never see each other's rows.
"""

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    owner     TEXT NOT NULL,
    id        TEXT NOT NULL,
    chat      TEXT NOT NULL,      -- 'u:<peer name>' or 'g:<group id>'
    direction TEXT NOT NULL,      -- 'in', 'out' or 'system'
    sender    TEXT NOT NULL,
    text      TEXT NOT NULL,
    ts        REAL NOT NULL,      -- seconds since 1970, used for ordering
    shown     TEXT NOT NULL,      -- the HH:MM:SS shown in the bubble
    status    TEXT NOT NULL DEFAULT 'delivered',   -- sending / queued / delivered / failed
    kind      TEXT NOT NULL DEFAULT 'text',        -- 'text' or 'file'
    extra     TEXT NOT NULL DEFAULT '{}',          -- JSON: group info, file details
    acked     INTEGER NOT NULL DEFAULT 0,          -- group messages: recipients that sent an ACK
    total     INTEGER NOT NULL DEFAULT 1,          -- ... out of this many
    PRIMARY KEY (owner, id)
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages (owner, chat, ts);
CREATE TABLE IF NOT EXISTS outbox (
    owner     TEXT NOT NULL,
    id        TEXT NOT NULL,
    recipient TEXT NOT NULL,
    fields    TEXT NOT NULL,      -- JSON of the MSG frame fields
    created   REAL NOT NULL,
    PRIMARY KEY (owner, id, recipient)
);
CREATE TABLE IF NOT EXISTS groups (
    owner   TEXT NOT NULL,
    id      TEXT NOT NULL,
    name    TEXT NOT NULL,
    members TEXT NOT NULL,        -- JSON list of usernames
    PRIMARY KEY (owner, id)
);
CREATE TABLE IF NOT EXISTS transfers (
    owner     TEXT NOT NULL,
    fid       TEXT NOT NULL,
    direction TEXT NOT NULL,      -- 'out' or 'in'
    peer      TEXT NOT NULL,
    name      TEXT NOT NULL,
    size      INTEGER NOT NULL,
    sha256    TEXT NOT NULL,
    path      TEXT NOT NULL,      -- source file (out) or partly written file (in)
    done      INTEGER NOT NULL,   -- bytes confirmed so far (only for display: a resume re-checks the file)
    updated   REAL NOT NULL,
    PRIMARY KEY (owner, fid)
);
CREATE TABLE IF NOT EXISTS peer_keys (
    owner   TEXT NOT NULL,
    name    TEXT NOT NULL,        -- peer username
    ident   TEXT NOT NULL,        -- hex of its Ed25519 public identity key
    first   REAL NOT NULL,
    PRIMARY KEY (owner, name)
);
"""

MESSAGE_COLUMNS = ("id", "chat", "direction", "sender", "text", "ts", "shown",
                   "status", "kind", "extra", "acked", "total")


def _row_to_message(row):
    message = dict(zip(MESSAGE_COLUMNS, row))
    message["extra"] = json.loads(message["extra"])
    return message


class Database:
    def __init__(self, path=":memory:", owner="me"):
        self.owner = owner
        self.lock = threading.Lock()
        # check_same_thread=False: we promise to use our own lock instead.
        self.db = sqlite3.connect(path, check_same_thread=False)
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def rename_owner(self, new_owner):
        """The user changed their username: keep the history, queue and groups under the new name."""
        with self.lock:
            for table in ("messages", "outbox", "groups", "peer_keys", "transfers"):
                self.db.execute("UPDATE OR IGNORE %s SET owner=? WHERE owner=?" % table, (new_owner, self.owner))
            self.db.commit()
            self.owner = new_owner

    def _run(self, sql, args=()):
        with self.lock:
            cursor = self.db.execute(sql, args)
            self.db.commit()
            return cursor

    def _query(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # ------------------------------------------------------------------
    # chat history
    # ------------------------------------------------------------------
    def add_message(self, message):
        """Store a message dict. Returns False if a message with this id already exists
        (a retry or a duplicate delivery), so callers can ignore it."""
        row = dict(id=None, chat=None, direction=None, sender="", text="", ts=time.time(),
                   shown=time.strftime("%H:%M:%S"), status="delivered", kind="text",
                   extra={}, acked=0, total=1)
        row.update(message)
        cursor = self._run(
            "INSERT OR IGNORE INTO messages (owner, id, chat, direction, sender, text, ts, shown,"
            " status, kind, extra, acked, total) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.owner, row["id"], row["chat"], row["direction"], row["sender"], row["text"],
             row["ts"], row["shown"], row["status"], row["kind"], json.dumps(row["extra"]),
             row["acked"], row["total"]))
        return cursor.rowcount == 1

    def get_message(self, message_id):
        rows = self._query("SELECT %s FROM messages WHERE owner=? AND id=?" % ",".join(MESSAGE_COLUMNS),
                           (self.owner, message_id))
        return _row_to_message(rows[0]) if rows else None

    def update_message(self, message_id, **columns):
        """Change some columns of one message, e.g. update_message(id, status='delivered')."""
        if "extra" in columns:
            columns["extra"] = json.dumps(columns["extra"])
        names = ", ".join("%s=?" % name for name in columns)
        self._run("UPDATE messages SET %s WHERE owner=? AND id=?" % names,
                  tuple(columns.values()) + (self.owner, message_id))

    def messages(self, chat, limit=500, search=None):
        """The newest `limit` messages of a chat, oldest first. `search` keeps only
        messages whose text contains that word (case-insensitive)."""
        sql = "SELECT %s FROM messages WHERE owner=? AND chat=?" % ",".join(MESSAGE_COLUMNS)
        args = [self.owner, chat]
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND direction != 'system' AND text LIKE ? ESCAPE '\\'"
            args.append("%" + escaped + "%")
        sql += " ORDER BY ts DESC, rowid DESC LIMIT ?"
        args.append(limit)
        return [_row_to_message(row) for row in reversed(self._query(sql, args))]

    def delete_message(self, message_id):
        self._run("DELETE FROM messages WHERE owner=? AND id=?", (self.owner, message_id))

    def clear_chat(self, chat):
        self._run("DELETE FROM messages WHERE owner=? AND chat=?", (self.owner, chat))

    def chat_summaries(self):
        """{chat: {'ts', 'preview'}} for the sidebar: the newest real message of every chat."""
        summaries = {}
        rows = self._query(
            "SELECT chat, ts, direction, sender, text, kind, extra FROM messages"
            " WHERE owner=? AND direction != 'system' ORDER BY ts ASC, rowid ASC", (self.owner,))
        for chat, ts, direction, sender, text, kind, extra in rows:
            if kind == "file":
                text = "File: " + json.loads(extra).get("name", "")
            if direction == "out":
                prefix = "You: "
            else:
                prefix = sender + ": " if chat.startswith("g:") else ""     # in a group: who said it
            summaries[chat] = {"ts": ts, "preview": prefix + text}
        return summaries

    def chats_with_messages(self):
        return {row[0] for row in self._query(
            "SELECT DISTINCT chat FROM messages WHERE owner=? AND direction != 'system'", (self.owner,))}

    def fail_stale_sending(self):
        """After a restart nothing is really 'sending' any more. Messages still in the outbox
        become 'queued' again (they will be re-sent), the others 'failed'."""
        self._run("UPDATE messages SET status='queued' WHERE owner=? AND status='sending' AND id IN"
                  " (SELECT id FROM outbox WHERE owner=?)", (self.owner, self.owner))
        self._run("UPDATE messages SET status='failed' WHERE owner=? AND status='sending'", (self.owner,))
        # A file transfer that was saved in `transfers` resumes later; any other one cannot survive a restart.
        resumable = {row[0] for row in self._query("SELECT fid FROM transfers WHERE owner=?", (self.owner,))}
        for message_id, extra in self._query("SELECT id, extra FROM messages WHERE owner=? AND kind='file'",
                                             (self.owner,)):
            info = json.loads(extra)
            if info.get("state") in ("hashing", "offered", "sending", "receiving", "verifying", "interrupted"):
                if info.get("fid", message_id) in resumable:
                    info.update(state="interrupted", reason="")
                else:
                    info.update(state="failed", reason="interrupted when the program was closed")
                self.update_message(message_id, extra=info)

    # ------------------------------------------------------------------
    # the outbox = offline queue (store and forward)
    # ------------------------------------------------------------------
    def outbox_add(self, message_id, recipient, fields):
        self._run("INSERT OR REPLACE INTO outbox (owner, id, recipient, fields, created) VALUES (?,?,?,?,?)",
                  (self.owner, message_id, recipient, json.dumps(fields), time.time()))

    def outbox_remove(self, message_id, recipient):
        """Returns True if an entry was removed."""
        cursor = self._run("DELETE FROM outbox WHERE owner=? AND id=? AND recipient=?",
                           (self.owner, message_id, recipient))
        return cursor.rowcount == 1

    def outbox_for(self, recipient):
        """[(message id, fields)] waiting for `recipient`, oldest first (= delivery order)."""
        rows = self._query("SELECT id, fields FROM outbox WHERE owner=? AND recipient=?"
                           " ORDER BY created ASC, rowid ASC", (self.owner, recipient))
        return [(message_id, json.loads(fields)) for message_id, fields in rows]

    def outbox_count(self, recipient=None):
        if recipient is None:
            return self._query("SELECT COUNT(*) FROM outbox WHERE owner=?", (self.owner,))[0][0]
        return self._query("SELECT COUNT(*) FROM outbox WHERE owner=? AND recipient=?",
                           (self.owner, recipient))[0][0]

    # ------------------------------------------------------------------
    # groups
    # ------------------------------------------------------------------
    def add_group(self, group_id, name, members):
        """Remember a group. Returns True if it was new."""
        cursor = self._run("INSERT OR IGNORE INTO groups (owner, id, name, members) VALUES (?,?,?,?)",
                           (self.owner, group_id, name, json.dumps(list(members))))
        return cursor.rowcount == 1

    def groups(self):
        return [{"id": gid, "name": name, "members": json.loads(members)}
                for gid, name, members in self._query(
                    "SELECT id, name, members FROM groups WHERE owner=?", (self.owner,))]

    # ------------------------------------------------------------------
    # peer identity keys (trust on first use)
    # ------------------------------------------------------------------
    def peer_key(self, name):
        """The identity key (hex) remembered for `name`, or None."""
        rows = self._query("SELECT ident FROM peer_keys WHERE owner=? AND name=?", (self.owner, name))
        return rows[0][0] if rows else None

    def pin_peer_key(self, name, ident):
        """Remember `ident` for `name` unless a key is already remembered. Returns the stored key.
        Check and insert happen under ONE lock, so two handshakes cannot both pin a key."""
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO peer_keys (owner, name, ident, first) VALUES (?,?,?,?)",
                            (self.owner, name, ident, time.time()))
            self.db.commit()
            return self.db.execute("SELECT ident FROM peer_keys WHERE owner=? AND name=?",
                                   (self.owner, name)).fetchone()[0]

    def set_peer_key(self, name, ident):
        """The user decided to trust a new key for `name` (e.g. that person reinstalled NetChat)."""
        self._run("INSERT OR REPLACE INTO peer_keys (owner, name, ident, first) VALUES (?,?,?,?)",
                  (self.owner, name, ident, time.time()))

    # ------------------------------------------------------------------
    # unfinished file transfers (resume)
    # ------------------------------------------------------------------
    TRANSFER_COLUMNS = ("fid", "direction", "peer", "name", "size", "sha256", "path", "done")

    def save_transfer(self, **row):
        values = [row[c] for c in self.TRANSFER_COLUMNS]
        self._run("INSERT OR REPLACE INTO transfers (owner, %s, updated) VALUES (?,%s,?)"
                  % (", ".join(self.TRANSFER_COLUMNS), ",".join("?" * len(values))),
                  [self.owner] + values + [time.time()])

    def update_transfer_done(self, fid, done):
        self._run("UPDATE transfers SET done=?, updated=? WHERE owner=? AND fid=?", (done, time.time(), self.owner, fid))

    def delete_transfer(self, fid):
        self._run("DELETE FROM transfers WHERE owner=? AND fid=?", (self.owner, fid))

    def saved_transfers(self):
        return [dict(zip(self.TRANSFER_COLUMNS, row)) for row in self._query(
            "SELECT %s FROM transfers WHERE owner=? ORDER BY updated" % ", ".join(self.TRANSFER_COLUMNS), (self.owner,))]
