from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


TEXT_SHA_SUFFIXES = {".py", ".json", ".yaml", ".yml", ".csv", ".md"}


def raw_file_sha256(path: Path) -> str:
    """Return the SHA256 of the file's physical bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalized_text_sha256(path: Path) -> str:
    """Return SHA256 for UTF-8 text after normalizing CRLF/CR to LF."""
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def canonical_json_content_sha256(payload: dict[str, Any]) -> str:
    """Return the repository's canonical JSON content SHA, excluding _run_info."""
    body = {key: value for key, value in payload.items() if key != "_run_info"}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def provenance_text_or_raw_sha256(path: Path) -> str:
    """Return normalized text SHA for known text artifacts, raw SHA otherwise."""
    resolved = Path(path)
    if resolved.suffix.lower() in TEXT_SHA_SUFFIXES:
        return normalized_text_sha256(resolved)
    return raw_file_sha256(resolved)
