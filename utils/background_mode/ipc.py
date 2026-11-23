"""Supervisor -> app messages (JSON lines on stdout, marked so stray prints are ignored)."""

from __future__ import annotations

import json
import sys
from typing import Any

from utils.background_mode.common import PROTOCOL_PREFIX


def emit(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    sys.stdout.write(PROTOCOL_PREFIX + json.dumps(payload) + "\n")
    sys.stdout.flush()


def log(message: str, level: str = "debug") -> None:
    emit("log", level=level, message=message)
