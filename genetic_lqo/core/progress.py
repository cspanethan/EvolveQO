from __future__ import annotations

import sys
from datetime import datetime
from typing import TextIO


def emit_progress(message: str, *, output: TextIO | None = None) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"timestamp={timestamp} {message}", file=output or sys.stdout, flush=True)
