from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

from .util import utc_now


class EventLogger:
    def __init__(self, path: Path, stage: str):
        self.path = path
        self.stage = stage
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, *, level: str = "info", console: bool = True, **fields: Any) -> None:
        record = {
            "timestamp": utc_now(),
            "level": level,
            "stage": self.stage,
            "event": event,
            **fields,
        }
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
        if console:
            print(encoded, file=sys.stderr)
