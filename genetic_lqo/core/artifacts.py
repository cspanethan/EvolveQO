from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path


def _canonical_json(value: object) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_json_text(value: object) -> str:
    return _canonical_json(value) + "\n"


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def write_json(path: Path | str, value: object) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def write_text(path: Path | str, content: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def create_experiment_directory(parent: Path | str, *, started_at: datetime | None = None) -> Path:
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    name = (started_at or datetime.now().astimezone()).strftime("%Y-%m-%dT%H-%M")
    ordinal = 1
    while True:
        candidate = parent / (name if ordinal == 1 else f"{name}-{ordinal:04d}")
        try:
            candidate.mkdir()
        except FileExistsError:
            ordinal += 1
            continue
        return candidate
