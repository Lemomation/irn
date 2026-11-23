"""Minimal sway IPC client (i3-ipc wire protocol)."""

from __future__ import annotations

import json
import socket
import struct
from typing import Any


class SwayIPC:
    MAGIC = b"i3-ipc"
    RUN_COMMAND = 0
    GET_TREE = 4

    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        self.sock.connect(path)

    def _recv_exact(self, size: int) -> bytes:
        chunks = []
        while size > 0:
            chunk = self.sock.recv(size)
            if not chunk:
                raise ConnectionError("sway IPC closed")
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)

    def request(self, msg_type: int, payload: str = "") -> Any:
        data = payload.encode()
        self.sock.sendall(self.MAGIC + struct.pack("<II", len(data), msg_type) + data)
        header = self._recv_exact(14)
        size, _reply_type = struct.unpack("<II", header[6:])
        return json.loads(self._recv_exact(size))

    def command(self, command: str) -> Any:
        return self.request(self.RUN_COMMAND, command)

    def tree(self) -> dict:
        return self.request(self.GET_TREE)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


def iter_views(node: dict):
    for child in list(node.get("nodes") or []) + list(node.get("floating_nodes") or []):
        if child.get("pid") and child.get("type") in ("con", "floating_con"):
            yield child
        yield from iter_views(child)

