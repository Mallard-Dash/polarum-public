"""Wake-on-LAN magic packets sent directly from this machine (UDP broadcast)."""
from __future__ import annotations

import re
import socket

WOL_PORT = 9
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def build_magic_packet(mac: str) -> bytes:
    """6 bytes of 0xFF followed by the MAC address repeated 16 times."""
    if not MAC_RE.fullmatch(mac):
        raise ValueError(f"invalid MAC address: {mac!r}")
    raw = bytes.fromhex(mac.replace(":", ""))
    return b"\xff" * 6 + raw * 16


def send_magic_packet(mac: str, broadcast: str, port: int = WOL_PORT) -> None:
    """Send one magic packet. Raises ValueError for a bad MAC, OSError on network errors."""
    packet = build_magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(5)
        sock.sendto(packet, (broadcast, port))
