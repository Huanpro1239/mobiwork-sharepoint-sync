"""Common environment and atomic manifest handling for report commands."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().casefold() in {"1", "true", "yes", "on"}


@contextmanager
def temporary_environment(settings: dict[str, str]):
    """Apply command-specific settings and restore the caller's environment."""
    previous = {name: os.environ.get(name) for name in settings}
    os.environ.update(settings)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def write_manifest(path: Path, payload: dict[str, Any]) -> Path:
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".json", delete=False) as handle:
        staged = Path(handle.name)
    try:
        staged.write_text(content, encoding="utf-8")
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)
    return path
