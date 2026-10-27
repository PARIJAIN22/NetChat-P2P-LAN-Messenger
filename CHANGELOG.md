# Changelog

Each entry names the **networking concept** it demonstrates, for the report.

## 2.0 - LAN messenger (protocol version 1 of the new TCP format)

### Protocol change (affects everything below)

* **TCP framing.** The old text line `TYPE|SENDER|TIMESTAMP|PAYLOAD\n` is replaced, for TCP only, by
  *4-byte big-endian length + UTF-8 JSON header (+ raw bytes for file chunks)*.
  *Concept: TCP is a byte stream without message boundaries; length-prefix framing; partial reads
  (`read_exactly` loops until all bytes arrived); binary-safe payloads; defensive size limits (1 MB header,
  256 KB chunk) checked before reading.*
* **Protocol version field** (`"v"`) in every frame; another version is answered with an `ERROR` frame and a
  clean close. *Concept: protocol versioning and graceful rejection.*
* UDP discovery (`HELLO` / `REPLY` / `BYE`) is **unchanged**.
* **Handshake** (`HI` frame in both directions) before any other frame. *Concept: connection set-up,
  capability negotiation.*
* Every TCP connection now works **in both directions** (reader thread per connection; replies on the same socket).
  *Concept: full-duplex TCP, request / response over one connection.*

### Tier 1 - a real messenger

1. **Delivery receipts.** `MSG` carries a uuid4 `id`; the receiver answers `ACK`. The bubble shows
   sending (clock), delivered (tick) or failed (red, click to retry; 10 s time-out).
   *Concept: application-level acknowledgements on top of TCP (TCP only proves the bytes reached the other
   computer's network stack, not that the program handled them); time-outs; retransmission with
   de-duplication by message id (at-least-once delivery).*
2. **Offline message queue.** Messages that cannot be sent wait in an sqlite outbox ("waiting for peer"),
   survive restarts, and are delivered in order when the peer's `HELLO` is seen again. A message is removed
   only when its ACK arrives. A connection that dies with unacknowledged messages re-queues them.
   *Concept: store and forward, soft-state discovery used as a trigger, ordering guarantees.*
3. **Chat history** in `netchat.db` (sqlite3), reloaded when a chat is opened; **search** in the current chat;
   **clear chat** with confirmation. *Concept: persistence on the client; no server holds any data.*
4. **Typing indicator.** `TYPING` frame, at most one per 2 s per peer, expires after 3 s on the receiver.
   *Concept: rate limiting of chatty control traffic; soft state with expiry.*
5. **Unread badges, last-message preview, chats sorted by recent activity.**

### Tier 2 - files

6. **File transfer**: `FILE_OFFER` (name, size, SHA-256) -> Accept / Decline -> `FILE_CHUNK` x N (64 KB) ->
   `FILE_DONE` -> `FILE_RESULT`; progress bar and speed on both sides; Cancel; saved to `Downloads\NetChat`.
   Streams files (a 500 MB transfer measured at ~206 MB/s on loopback with ~5 MB extra memory). Chat keeps working
   during a transfer because every frame holds the send lock only for itself.
   *Concepts: flow control by chunking (TCP window + `sendall()` blocking), integrity check by hash, multiplexing
   control and bulk data on one connection, back-pressure, never loading data into memory, path-traversal
   defence on received file names.*
7. **Attach button** (paperclip, Ctrl+O), PNG / GIF thumbnails with tkinter `PhotoImage`, file icon otherwise,
   click a received file to open its folder.

### Tier 3 - groups and security

9. **Group chat**: the "Everyone" room (fan-out to every online peer over the existing connections) and named
   groups. Every message carries `group {id, name, members}`, so members who never heard of the group create it
   on arrival. Delivery shows `2/3` per group message; offline members get theirs from the queue.
   *Concept: application-level multicast by unicast fan-out; self-describing messages (no group server).*
10. **Encryption** of all TCP traffic: X25519 key exchange per connection, HKDF-SHA256, AES-256-GCM records with
    counter nonces, Ed25519 identity signatures, key-confirmation (`HI` / `AUTH`). Lock icon in the chat header.
    Optional **shared passphrase** mixed into the key derivation; mismatches are rejected with a clear reason.
    TLS was not used because the standard `ssl` module cannot create certificates; the optional `cryptography`
    package is the one allowed dependency. *Concepts: confidentiality, integrity, authenticated encryption,
    Diffie-Hellman, forward secrecy, replay protection, downgrade attacks, pre-shared secrets.*
11. **Key fingerprint** (80 bits of SHA-256 of the public key) shown next to the lock for out-of-band comparison.
    *Concept: man-in-the-middle detection by comparing fingerprints.*

### Tier 4 - extras

12. Copy message (right-click), delete message locally, **emoji picker** (40 emojis; they travel as UTF-8 JSON).
13. **Settings dialog** persisted in `settings.json` (username, ports, download folder, sound, dark mode,
    passphrase); command line > settings > defaults; written atomically; "Restart now" re-launches the program.
14. **Network info panel**: my IP, subnet mask, network and broadcast address, peers with last-seen time, round-trip
    time and fingerprint; *Copy as text* for the report. **RTT** is measured with `PING` / `PONG` every 15 s
    over the established TCP connection (the clock starts after the connection exists).
    *Concepts: IP addressing, subnet mask, limited vs directed broadcast, round-trip time measurement.*

### Console mode

* `--cli` now also has `/send NAME text`, `/file NAME path`, `/accept`, `/decline`, `/cancel`, `/transfers`;
  `/peers` shows address, RTT and encryption. The offline queue works there too. (`/to` and `/msg` still work.)

### Structure and tests

* New modules: `connection.py`, `crypto.py`, `storage.py`, `filetransfer.py`, `settings.py`, `netinfo.py`;
  the window is split into `gui.py` plus mixins (`gui_layout`, `gui_status`, `gui_files`, `gui_groups`,
  `gui_settings`) and `gui_chatview`, `gui_dialogs`, `gui_widgets`.
* New options: `--data-dir`, `--download-dir`, `--passphrase`, `--no-encryption`.
* Tests grew from 20 to 105: every frame type, partial reads, oversized frames, bad JSON, wrong version, ACK and
  queue behaviour, 10 MB file transfer with hash comparison, a recording proxy that shows the wire is encrypted,
  storage, settings, RTT.

### Bugs found by the new tests and fixed during development

* A `PING` field called `t` clashed with the reserved frame-type field (found by the all-frame-types test).
* A message queued while a flush was running could be forgotten until the next HELLO (race in `flush_outbox`;
  now a "flush again" flag is set and checked under one lock).
* Clearing "pending" before the outbox row on an ACK let a new message queue behind a message that was already delivered.
* A connection reset with unread data lost messages already "sent": unacknowledged messages are now re-queued.
* `settings.load` rejected every boolean, so dark mode and sound would never have been remembered.
* The first PING's round-trip time included the TCP connect and key exchange (~47 ms on loopback instead of ~1 ms).
* "Retry" on a message that had just been acknowledged left it stuck on "sending".

## 1.1 - restyled window

New look (navy / teal, light and dark mode), chat bubbles, peer list with avatars and unread badges, network log
panel, start-up dialog, taskbar icon, high-DPI support. No change to networking.

## 1.0 - first version

Peer-to-peer text chat over TCP with UDP broadcast discovery, tkinter window and console mode, socket log.
