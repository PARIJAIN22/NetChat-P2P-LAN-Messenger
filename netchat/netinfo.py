"""
netinfo.py - facts about this computer's network, for the "Network info" panel.

Python's standard library cannot tell us the subnet mask of the network card (it only knows
our IP address), so on Windows we ask PowerShell once. If that fails (or on other systems) we
ASSUME a /24 network, which is what home routers and phone hotspots use, and say so on screen.
"""

import ipaddress
import subprocess

from . import config


def subnet_details(ip, prefix):
    """Work out the mask, the network address and the broadcast address from an IP and a prefix length."""
    interface = ipaddress.ip_interface("%s/%d" % (ip, prefix))
    network = interface.network
    return {
        "ip": ip,
        "prefix": prefix,
        "netmask": str(network.netmask),
        "network": str(network.network_address),
        "directed_broadcast": str(network.broadcast_address),       # e.g. 192.168.1.255
        "discovery_broadcast": config.BROADCAST_ADDRESS,            # what NetChat really sends HELLO to
    }


def detect_prefix_length(ip, timeout=6):
    """(prefix length, 'adapter' or 'assumed')."""
    try:
        address = ipaddress.ip_address(ip)                          # also protects the command below
    except ValueError:
        return 24, "assumed"
    if address.is_loopback:
        return 8, "adapter"
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-NetIPAddress -IPAddress '%s' -AddressFamily IPv4 -ErrorAction Stop).PrefixLength" % address],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        prefix = int(result.stdout.strip().splitlines()[0])
        if 1 <= prefix <= 32:
            return prefix, "adapter"
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return 24, "assumed"


def get_network_info(ip):
    """Everything for the panel. Slow (starts PowerShell): call it from a background thread."""
    prefix, source = detect_prefix_length(ip)
    info = subnet_details(ip, prefix)
    info["source"] = source
    return info
