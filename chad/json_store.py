"""Shared JSON-persistence helpers.

Every persistent store in Chad has the same three requirements, and the
first M2 adversarial review found history.py and proposals.py had
already drifted on the second one:

  1. Missing file    → return empty.
  2. Corrupt file    → rename aside with a timestamp, return empty. The
                       broken file is preserved for inspection; the next
                       save cannot destroy it.
  3. Atomic save     → write to a temp file, os.replace into position.
                       Never leaves the file half-written.

This module owns those three so callers can't get any of them wrong,
and can't drift apart on the fixes.

Not fixed here: crash *durability*. Without fsync on the tempfile and
its parent directory, a power cut can lose the last write. Fine at this
scale; documented for later reference.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def to_plain(value: Any) -> Any:
    """Recursively convert pydantic blocks to dicts so json.dumps works.

    Anthropic tool_use / tool_result / text blocks arrive as pydantic
    objects; json.dumps rejects them without a converter.
    """
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    return value


def load_json_dict(path: Path, log: logging.Logger) -> dict:
    """Load a JSON object from path with corruption tolerance.

    Missing file → {}. Corrupt file → rename aside as
    <name>.corrupt.<utc-timestamp> and return {}. Any exception during
    the rename is logged but not raised — the store must always start.
    """
    if not path.exists():
        log.info("No file at %s — starting empty.", path)
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("file is not a JSON object")
        return data
    except (json.JSONDecodeError, ValueError, OSError) as e:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        preserved = path.with_name(f"{path.name}.corrupt.{ts}")
        try:
            os.rename(path, preserved)
            log.warning(
                "File at %s is unusable (%s). Preserved as %s. Starting empty.",
                path, e, preserved,
            )
        except OSError as rename_err:
            log.warning(
                "File at %s is unusable (%s) and could not be preserved (%s). "
                "Starting empty; the broken file will be overwritten on next save.",
                path, e, rename_err,
            )
        return {}


def save_json_dict_atomic(path: Path, data: dict) -> None:
    """Write data to path atomically.

    Uses to_plain() so pydantic blocks in the payload don't blow up
    json.dumps mid-save and leave memory + disk out of sync.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(to_plain(data), ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)
