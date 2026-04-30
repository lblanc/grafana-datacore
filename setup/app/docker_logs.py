"""
Streaming and tailing logs from the collector container via the Docker
Engine API. Avoids shelling out to docker CLI; speaks the daemon's
multiplexed stdout/stderr framing directly.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Iterator, Tuple

LOGGER = logging.getLogger(__name__)

DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
COLLECTOR_NAME = os.environ.get("COLLECTOR_CONTAINER", "grafana-datacore-collector")


def _connect() -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(30)
    sock.connect(DOCKER_SOCKET)
    return sock


def _send_request(sock: socket.socket, path: str) -> None:
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: docker\r\n"
        f"Accept: */*\r\n"
        f"Connection: close\r\n\r\n"
    )
    sock.sendall(request.encode("ascii"))


def _read_headers(sock: socket.socket) -> Tuple[int, dict, bytes]:
    """Read response status + headers; return (status_code, headers, leftover_body)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, leftover = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_line = lines[0].decode("ascii", "replace")
    parts = status_line.split(" ", 2)
    status_code = int(parts[1]) if len(parts) > 1 else 0
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.decode("ascii", "replace").strip().lower()] = v.decode(
                "ascii", "replace"
            ).strip()
    return status_code, headers, leftover


def _iter_demuxed(sock: socket.socket, leftover: bytes) -> Iterator[bytes]:
    """Iterate over Docker's multiplexed stream frames.

    Frame format: [stream(1) 0 0 0 size(4)] payload
    where stream is 0=stdin, 1=stdout, 2=stderr.
    """
    buffer = leftover
    while True:
        while len(buffer) < 8:
            chunk = sock.recv(8192)
            if not chunk:
                if buffer:
                    yield buffer
                return
            buffer += chunk
        header = buffer[:8]
        size = int.from_bytes(header[4:8], "big")
        buffer = buffer[8:]
        while len(buffer) < size:
            chunk = sock.recv(8192)
            if not chunk:
                yield buffer
                return
            buffer += chunk
        yield buffer[:size]
        buffer = buffer[size:]


def _iter_chunked(sock: socket.socket, leftover: bytes) -> Iterator[bytes]:
    """Iterate over an HTTP/1.1 chunked-transfer-encoded stream."""
    buffer = leftover
    while True:
        # Read chunk size line
        while b"\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                return
            buffer += chunk
        size_line, _, buffer = buffer.partition(b"\r\n")
        try:
            size = int(size_line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            return
        if size == 0:
            return
        while len(buffer) < size + 2:  # +2 for trailing \r\n
            chunk = sock.recv(8192)
            if not chunk:
                yield buffer[:size] if buffer else b""
                return
            buffer += chunk
        yield buffer[:size]
        buffer = buffer[size + 2 :]


def tail_logs(tail: int = 200) -> Tuple[bool, str]:
    """One-shot: return the last ``tail`` lines, decoded as text."""
    try:
        sock = _connect()
    except OSError as exc:
        return False, f"Could not reach Docker daemon: {exc}"

    try:
        path = (
            f"/containers/{COLLECTOR_NAME}/logs"
            f"?stdout=1&stderr=1&tail={tail}&timestamps=0"
        )
        _send_request(sock, path)
        status, headers, leftover = _read_headers(sock)
        if status == 404:
            return False, f"Container {COLLECTOR_NAME} not found."
        if status != 200:
            return False, f"Docker API returned HTTP {status}."

        # The /logs endpoint returns a multiplexed stream when the container
        # was started without a TTY (which is our case).
        chunks: list[bytes] = []
        is_chunked = headers.get("transfer-encoding", "").lower() == "chunked"
        is_demuxed = "application/vnd.docker.multiplexed-stream" in headers.get(
            "content-type", ""
        ) or not is_chunked
        if is_chunked:
            for piece in _iter_chunked(sock, leftover):
                chunks.append(piece)
            raw = b"".join(chunks)
            # Even chunked bodies can carry the multiplexed framing.
            if raw.startswith((b"\x01", b"\x02")):
                # Re-parse demuxed framing inside the assembled body.
                raw = _strip_demux_framing(raw)
        else:
            for piece in _iter_demuxed(sock, leftover):
                chunks.append(piece)
            raw = b"".join(chunks)

        return True, raw.decode("utf-8", errors="replace")
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _strip_demux_framing(raw: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(raw)
    while i + 8 <= n:
        size = int.from_bytes(raw[i + 4 : i + 8], "big")
        i += 8
        out.extend(raw[i : i + size])
        i += size
    return bytes(out)


def stream_logs(tail: int = 100) -> Iterator[str]:
    """Generator: yield log lines as they arrive (for SSE / streaming endpoints)."""
    try:
        sock = _connect()
    except OSError as exc:
        yield f"[setup] Could not reach Docker daemon: {exc}\n"
        return

    try:
        path = (
            f"/containers/{COLLECTOR_NAME}/logs"
            f"?stdout=1&stderr=1&follow=1&tail={tail}&timestamps=0"
        )
        _send_request(sock, path)
        status, headers, leftover = _read_headers(sock)
        if status != 200:
            yield f"[setup] Docker API returned HTTP {status}\n"
            return

        # follow=1 returns demuxed frames continuously.
        partial = b""
        for piece in _iter_demuxed(sock, leftover):
            data = partial + piece
            partial = b""
            *complete, tail_part = data.split(b"\n")
            for line in complete:
                yield line.decode("utf-8", errors="replace") + "\n"
            partial = tail_part
        if partial:
            yield partial.decode("utf-8", errors="replace")
    finally:
        try:
            sock.close()
        except OSError:
            pass
