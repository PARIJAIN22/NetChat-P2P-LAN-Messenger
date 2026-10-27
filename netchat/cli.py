"""
cli.py - console mode. Same networking as the GUI (text, files, offline queue), useful
for testing and as a backup for the demo.

Commands:
  /peers                list online peers
  /send NAME text       send a message to NAME (kept and delivered later if NAME is offline)
  /file NAME path       send a file to NAME
  /accept ID            accept an offered file (ID is shown in the offer)
  /decline ID           refuse an offered file
  /cancel ID            stop a running transfer
  /transfers            list file transfers
  /connect IP[:PORT]    connect by address when discovery does not find a peer
  /trust NAME           accept the NEW identity key NAME presented (after comparing fingerprints)
  /to NAME              choose who plain text is sent to
  /msg NAME text        same as /send
  /help                 show this help
  /quit                 leave
Anything else you type is sent to the peer chosen with /to.
"""

import os
import time
import uuid

from . import config
from .logger import EventLog
from .peer import Peer
from .storage import Database


def _size(count):
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return "%d %s" % (count, unit) if unit == "B" else "%.1f %s" % (count, unit)
        count /= 1024.0


def run_cli(args):
    log = EventLog("%s:%d" % (args.name or "?", args.tcp_port), args.log_file)
    if args.verbose:
        log.add_listener(lambda line: print("   [log] " + line))

    name = args.name
    if not name:
        try:
            name = input("Username: ").strip()
        except EOFError:
            return 1
    name = name[:config.MAX_NAME_LEN]
    if not name:
        print("A username is required.")
        return 1
    log.label = "%s:%d" % (name, args.tcp_port)

    data_dir = getattr(args, "data_dir", ".")
    os.makedirs(data_dir, exist_ok=True)
    db = Database(os.path.join(data_dir, config.DB_FILE), name)      # the offline queue survives restarts

    def show_message(sender, text, timestamp):
        print("\n[%s] %s: %s" % (timestamp, sender, text))

    last_percent = {}
    presented = {}                                # peer -> new identity key it presented (for /trust)

    def show_event(kind, info):
        """Events from the network layer (they arrive in a network thread; printing is safe)."""
        if kind == "file_offer":
            print("\n[file] %s offers '%s' (%s). Type /accept %s or /decline %s"
                  % (info["peer"], info["name"], _size(info["size"]), info["fid"][:6], info["fid"][:6]))
        elif kind == "file_state" and info["state"] == "interrupted":
            print("\n[file] '%s' interrupted (%s); it continues automatically" % (info["name"], info["reason"]))
        elif kind == "file_state" and info["state"] in ("verified", "failed", "declined", "cancelled"):
            detail = {"verified": "done, SHA-256 verified", "failed": "FAILED: " + info["reason"]}.get(info["state"], info["state"])
            where = " -> " + info["path"] if info["state"] == "verified" and info["direction"] == "in" else ""
            print("\n[file] %s '%s' %s%s" % ("from" if info["direction"] == "in" else "to", info["name"], detail, where)
                  + " (" + info["peer"] + ")")
        elif kind == "file_progress" and info["size"]:
            percent = int(100 * info["done"] / info["size"]) // 25 * 25      # report 0, 25, 50, 75, 100 %
            if last_percent.get(info["fid"]) != percent:
                last_percent[info["fid"]] = percent
                print("[file] '%s' %d%% (%s/s)" % (info["name"], percent, _size(info["speed"])))
        elif kind == "msg_status" and info["status"] in ("queued", "failed"):
            print("[status] message to %s: %s" % (info["peer"], "waiting for the peer to come back"
                                                  if info["status"] == "queued"
                                                  else "failed: " + (info.get("reason") or "no acknowledgement")))
        elif kind == "identity":
            presented[info["peer"]] = info["ident"]
            print("\n[WARNING] %s presents a different key (%s, remembered %s). Reinstall or impersonator? "
                  "Compare fingerprints, then type /trust %s to accept it."
                  % (info["peer"], info["presented"], info["known"], info["peer"]))

    peer = Peer(name, args.tcp_port, args.udp_port, log,
                on_message=show_message,
                on_peers_changed=lambda: None,
                on_status=lambda text: print("[status] " + text),
                on_event=show_event, db=db,
                download_dir=getattr(args, "download_dir", None),
                passphrase=getattr(args, "passphrase", None) or "",
                identity_path=os.path.join(data_dir, "identity.key"),
                encrypt=not getattr(args, "no_encryption", False))
    try:
        peer.start()
    except OSError as e:
        print("Could not start NetChat: %s" % e)
        print("Is another NetChat already using TCP port %d or UDP port %d? "
              "Try --tcp-port / --udp-port." % (args.tcp_port, args.udp_port))
        return 1
    for address in getattr(args, "peer", None) or []:
        peer.connect_async(address)

    def find_transfer(prefix):
        matches = [fid for fid in list(peer.files.transfers) if fid.startswith(prefix)]
        return matches[0] if len(matches) == 1 else None

    def send_text(target, text):
        if not text.strip():
            return
        if len(text.strip()) > config.MAX_TEXT_LEN:
            print("Message too long (max %d characters)." % config.MAX_TEXT_LEN)
            return
        fields = {"text": text.strip(), "ts": time.strftime("%H:%M:%S")}
        result = peer.deliver(target, uuid.uuid4().hex, fields)
        print("[%s] me -> %s: %s%s" % (time.strftime("%H:%M:%S"), target, text,
                                        "" if result == "sent" else "   (%s)" % result))

    print("NetChat started as '%s' on %s:%d. Type /help for commands." % (name, peer.my_ip, args.tcp_port))
    selected = None
    try:
        while True:
            try:
                line = input().strip()
            except EOFError:
                break
            if line == "":
                continue
            if line == "/quit":
                break
            elif line == "/help":
                print(__doc__)
            elif line == "/peers":
                stats = peer.peer_stats()
                if not stats:
                    print("Online peers: (nobody yet)")
                for s in stats:
                    lock = ("encrypted, fingerprint " + s["fingerprint"]) if s["encrypted"] else "not encrypted / not connected yet"
                    rtt = "%.1f ms" % (s["rtt"] * 1000) if s["rtt"] is not None else "-"
                    print("  %s  %s:%d  rtt %s  [%s]" % (s["name"], s["ip"], s["port"], rtt, lock))
                print("My fingerprint: %s" % (peer.fingerprint or "(encryption off)"))
            elif line == "/transfers":
                for t in list(peer.files.transfers.values()):
                    print("  %s %s %s '%s' %s %d/%d" % (t.fid[:6], "->" if t.direction == "out" else "<-", t.peer,
                                                         t.name, t.state, t.done, t.size))
            elif line.startswith("/connect "):
                peer.connect_async(line[9:])
            elif line.startswith("/trust "):
                who = line[7:].strip()
                if who in presented:
                    peer.trust_identity(who, presented.pop(who))
                    print("New key for %s trusted." % who)
                else:
                    print("%s has not presented a new key." % who)
            elif line.startswith("/to "):
                selected = line[4:].strip()
                print("Now chatting with '%s'." % selected)
            elif line.startswith("/send ") or line.startswith("/msg "):
                parts = line.split(" ", 2)
                if len(parts) < 3:
                    print("Usage: /send NAME text")
                else:
                    send_text(parts[1], parts[2])
            elif line.startswith("/file "):
                parts = line.split(" ", 2)
                if len(parts) < 3 or not os.path.isfile(parts[2].strip('"')):
                    print("Usage: /file NAME path   (the path must be an existing file)")
                else:
                    fid = peer.files.offer(parts[1], parts[2].strip('"'))
                    print("[file] offering '%s' to %s (id %s)" % (os.path.basename(parts[2]), parts[1], fid[:6]))
            elif line.startswith(("/accept ", "/decline ", "/cancel ")):
                command, prefix = line.split(" ", 1)
                fid = find_transfer(prefix.strip())
                if fid is None:
                    print("No transfer with id '%s' (see /transfers)." % prefix)
                elif command == "/accept":
                    path = peer.files.accept(fid)
                    print("Saving to %s" % path if path else "Cannot accept that transfer.")
                elif command == "/decline":
                    peer.files.decline(fid)
                else:
                    peer.files.cancel(fid)
            elif line.startswith("/"):
                print("Unknown command. Type /help.")
            elif selected is None:
                print("Choose a peer first with /to NAME (see /peers).")
            else:
                send_text(selected, line)
    except KeyboardInterrupt:
        pass
    finally:
        peer.stop()
        db.close()
    print("Bye.")
    return 0
