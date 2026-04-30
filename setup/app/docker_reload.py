"""
Tell the collector to reload its configuration by sending it SIGHUP.

The setup container has the Docker socket mounted so it can signal sibling
containers without needing to be on the same Linux PID namespace.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from http.client import HTTPConnection
from typing import Tuple


LOGGER = logging.getLogger(__name__)

DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
COLLECTOR_NAME = os.environ.get("COLLECTOR_CONTAINER", "grafana-datacore-collector")


class _UnixHTTPConnection(HTTPConnection):
    """HTTPConnection variant that talks over a UNIX socket."""

    def __init__(self, socket_path: str, timeout: float = 10.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:  # type: ignore[override]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def reload_collector(signal_name: str = "HUP") -> Tuple[bool, str]:
    """Send a signal to the collector container.

    Uses the Docker Engine API. Returns ``(ok, human_readable_message)``.
    """
    if not os.path.exists(DOCKER_SOCKET):
        return False, (
            f"Docker socket not available at {DOCKER_SOCKET}. "
            "Mount /var/run/docker.sock in the setup service to enable reload."
        )

    conn = _UnixHTTPConnection(DOCKER_SOCKET)
    try:
        path = f"/containers/{COLLECTOR_NAME}/kill?signal={signal_name}"
        conn.request("POST", path)
        response = conn.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        if response.status in (204,):
            return True, f"Signal {signal_name} delivered to {COLLECTOR_NAME}."
        if response.status == 404:
            return False, f"Container {COLLECTOR_NAME} not found."
        if response.status == 409:
            # Container is not running; try restart instead.
            return _restart_container(conn)
        return False, f"Docker API returned {response.status}: {body[:200]}"
    except OSError as exc:
        return False, f"Could not reach Docker daemon: {exc}"
    finally:
        conn.close()


def _restart_container(conn: _UnixHTTPConnection) -> Tuple[bool, str]:
    path = f"/containers/{COLLECTOR_NAME}/restart"
    conn.request("POST", path)
    response = conn.getresponse()
    response.read()
    if response.status == 204:
        return True, f"{COLLECTOR_NAME} was stopped, restarted instead."
    return False, f"Restart failed with HTTP {response.status}."
