from __future__ import annotations

import csv
import hashlib
import re
from datetime import datetime
from pathlib import Path

from src.core.contracts.loader import PROJECT_ROOT, ContractLoadError
from src.core.mapping.models import SourceFieldProfile


SAMPLE_LIMIT = 5


class SourceProfileError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def source_sha256(path: Path) -> str:
    return hashlib.sha256(path.resolve().read_bytes()).hexdigest()


def _is_url(value: str) -> bool:
    normalized = value.lower().replace("\\", "/")
    return normalized.startswith((
        "http:/",
        "https:/",
        "ftp:/",
        "s3:/",
    ))


def _safe_source_path(path: Path) -> Path:
    text = str(path)
    if _is_url(text):
        raise SourceProfileError("remote_source_not_allowed", "Source CSV must be local")
    candidate = Path(path)
    if ".." in candidate.parts:
        raise SourceProfileError("path_escape_not_allowed", "Source CSV must not use path escape")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise SourceProfileError("path_outside_project", "Source CSV must be inside the project") from exc
    if not resolved.exists():
        raise SourceProfileError("source_missing", "Source CSV does not exist")
    return resolved


def _is_missing(value: str | None) -> bool:
    return value is None or value.strip() == ""


def _is_integer(value: str) -> bool:
    return bool(re.fullmatch(r"[-+]?\d+", value.strip()))


def _is_number(value: str) -> bool:
    return bool(re.fullmatch(r"[-+]?\d+(\.\d+)?", value.strip()))


def _is_boolean(value: str) -> bool:
    return value.strip().lower() in {"true", "false", "yes", "no", "y", "n", "0", "1"}


def _is_date(value: str) -> bool:
    try:
        datetime.strptime(value.strip(), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _is_datetime(value: str) -> bool:
    text = value.strip().replace("Z", "+00:00")
    try:
        datetime.fromisoformat(text)
        return "T" in text or " " in text
    except ValueError:
        return False


def _infer_kind(values: list[str]) -> str:
    present = [value for value in values if not _is_missing(value)]
    if not present:
        return "string"
    checks = [
        ("boolean", _is_boolean),
        ("integer", _is_integer),
        ("number", _is_number),
        ("datetime", _is_datetime),
        ("date", _is_date),
    ]
    for kind, check in checks:
        if all(check(value) for value in present):
            return kind
    return "string"


def _stable_samples(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    samples: list[str] = []
    for value in values:
        if _is_missing(value):
            continue
        normalized = value.strip()
        if normalized in seen:
            continue
        seen.add(normalized)
        samples.append(normalized)
        if len(samples) >= SAMPLE_LIMIT:
            break
    return tuple(samples)


def profile_source_csv(source_path: Path) -> tuple[list[SourceFieldProfile], dict[str, int | str]]:
    resolved = _safe_source_path(source_path)
    with resolved.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise SourceProfileError("empty_source", "Source CSV is empty") from exc
        if not header:
            raise SourceProfileError("empty_header", "Source CSV has no columns")
        if len(header) != len(set(header)):
            raise SourceProfileError("duplicate_columns", "Source CSV has duplicate columns")
        rows = list(reader)
    row_count = len(rows)
    if row_count == 0:
        raise SourceProfileError("empty_source", "Source CSV has no data rows")
    columns: dict[str, list[str]] = {name: [] for name in header}
    for row in rows:
        padded = row + [""] * (len(header) - len(row))
        for index, name in enumerate(header):
            columns[name].append(padded[index] if index < len(padded) else "")
    profiles: list[SourceFieldProfile] = []
    for name in header:
        values = columns[name]
        present = [value for value in values if not _is_missing(value)]
        distinct = []
        seen = set()
        for value in present:
            normalized = value.strip()
            if normalized not in seen:
                seen.add(normalized)
                distinct.append(normalized)
        profiles.append(
            SourceFieldProfile(
                name=name,
                inferred_kind=_infer_kind(values),
                row_count=row_count,
                present_count=len(present),
                missing_count=row_count - len(present),
                missing_ratio=round((row_count - len(present)) / row_count, 4),
                distinct_count=len(distinct),
                distinct_ratio=round(len(distinct) / len(present), 4) if present else 0.0,
                observed_max_length=max((len(value.strip()) for value in present), default=0),
                samples=_stable_samples(values),
            )
        )
    return profiles, {
        "source_path": project_relative(resolved),
        "source_sha256": source_sha256(resolved),
        "source_row_count": row_count,
        "source_field_count": len(header),
    }
