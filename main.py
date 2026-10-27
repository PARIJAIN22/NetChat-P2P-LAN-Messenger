"""
main.py - entry point. Reads the command-line options and starts GUI or CLI mode.

Examples:
    python main.py
    python main.py --name Rahul --tcp-port 5000
    python main.py --name Asha  --tcp-port 5002 --cli
    python main.py --name Meera --tcp-port 5003 --data-dir meera     (own history + settings)

Where a value comes from:   command-line option  >  settings.json  >  default in config.py
"""

import argparse
import os
import sys

from netchat import config, settings


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="NetChat - peer-to-peer LAN chat")
    p.add_argument("--name", help="your username (default: from settings.json, else asked in a dialog)")
    p.add_argument("--tcp-port", type=int, default=None,
                   help="TCP port for chat messages (default %d)" % config.DEFAULT_TCP_PORT)
    p.add_argument("--udp-port", type=int, default=None,
                   help="UDP port for discovery (default %d). Must be the same on all peers." % config.DEFAULT_UDP_PORT)
    p.add_argument("--cli", action="store_true", help="console mode instead of the GUI")
    p.add_argument("--verbose", action="store_true", help="(CLI) also print the socket log")
    p.add_argument("--log-file", default=config.LOG_FILE, help="log file (default %(default)s)")
    p.add_argument("--passphrase", default=None,
                   help="shared room passphrase: only peers with the same one can talk to you "
                        "(visible in the process list; the Settings dialog is safer)")
    p.add_argument("--no-encryption", action="store_true",
                   help="do not encrypt TCP traffic (for the demo: show what the wire looks like without it)")
    p.add_argument("--download-dir", default=None,
                   help="where received files are saved (default: your Downloads\\NetChat folder)")
    p.add_argument("--peer", action="append", default=[], metavar="IP[:PORT]",
                   help="connect to this NetChat by address at start-up (repeatable); for networks where "
                        "UDP discovery does not work")
    p.add_argument("--data-dir", default=".",
                   help="folder for netchat.db (history + offline queue) and settings.json (default: "
                        "current folder). Two instances on one PC need DIFFERENT folders.")
    args = p.parse_args(argv)
    apply_settings(args)
    return args


def apply_settings(args):
    """Fill every option that was NOT given on the command line from settings.json / config.py."""
    args.settings_path = os.path.join(args.data_dir, config.SETTINGS_FILE)
    args.settings = settings.load(args.settings_path)
    s = args.settings
    args.name = args.name or s["username"] or None
    args.tcp_port = args.tcp_port or s["tcp_port"]
    args.udp_port = args.udp_port or s["udp_port"]
    args.download_dir = args.download_dir or s["download_dir"] or None
    args.passphrase = args.passphrase if args.passphrase is not None else (s["passphrase"] or None)


def main():
    args = parse_args()
    # Import here so --help works even if tkinter is missing.
    if args.cli:
        from netchat.cli import run_cli
        return run_cli(args)
    from netchat.gui import run_gui
    return run_gui(args)


if __name__ == "__main__":
    sys.exit(main())
