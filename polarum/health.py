"""TCP connect health checks (no ICMP, no root needed)."""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass

from .config import Node
from .state import classify_ports

CONNECT_TIMEOUT = 1.0


@dataclass(frozen=True)
class HealthResult:
    health: str
    latency_ms: float | None


def check_port(host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> float | None:
    """Return connect time in ms, or None if the port is not reachable."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return None
    return (time.perf_counter() - start) * 1000.0


def check_node(node: Node, timeout: float = CONNECT_TIMEOUT) -> HealthResult:
    times = [check_port(node.ip, port, timeout) for port in node.health_ports]
    up = [t is not None for t in times]
    reachable = [t for t in times if t is not None]
    return HealthResult(classify_ports(up), min(reachable) if reachable else None)
