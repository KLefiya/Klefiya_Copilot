from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.core.contracts.loader import PROJECT_ROOT
from src.core.package_generation.models import LineageEntry
from src.tools.data_profile import attach_run_info


def project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def value_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_lineage_document(
    *,
    contract_id: str,
    source_path: Path,
    source_sha256: str,
    decision_path: Path,
    decision_sha256: str,
    entries: list[LineageEntry],
) -> dict[str, Any]:
    sorted_entries = sorted(
        entries,
        key=lambda item: (
            item.target_resource,
            item.target_row_number,
            item.target_field,
            item.source_row_number,
            item.source_field,
        ),
    )
    body = {
        "_meta": {
            "component": "migration_package_lineage",
            "contract_id": contract_id,
            "source_path": project_relative(source_path),
            "source_sha256": source_sha256,
            "decision_path": project_relative(decision_path),
            "decision_sha256": decision_sha256,
            "sensitive_values_included": False,
        },
        "entries": [entry.to_dict() for entry in sorted_entries],
    }
    return attach_run_info(body)


def write_lineage_document(document: dict[str, Any], output_path: Path) -> None:
    next_document = deepcopy(document)
    if output_path.exists():
        try:
            previous = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
        previous_run = previous.get("_run_info", {})
        next_run = next_document.get("_run_info", {})
        if (
            previous_run.get("content_sha256")
            and previous_run.get("content_sha256") == next_run.get("content_sha256")
            and previous_run.get("generated_at")
        ):
            next_document["_run_info"]["generated_at"] = previous_run["generated_at"]
    if os.environ.get("CARVEOPS_OMIT_TIMESTAMP") == "1":
        next_document.get("_run_info", {}).pop("generated_at", None)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(next_document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
