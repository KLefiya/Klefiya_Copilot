from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.core.contracts.loader import PROJECT_ROOT, LoadedMigrationContract
from src.core.package_generation.models import TargetResourceBuild
from src.tools.data_profile import attach_run_info


def project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def build_package_manifest(
    *,
    contract: LoadedMigrationContract,
    source_path: Path,
    source_sha256: str,
    source_row_count: int,
    mapping_report_path: Path,
    mapping_report_sha256: str,
    decision_path: Path,
    decision_sha256: str,
    resources: list[TargetResourceBuild],
    lineage_path: Path,
    lineage_sha256: str,
    lineage_entry_count: int,
    rejected_row_count: int,
) -> dict[str, Any]:
    body = {
        "contract": {
            "contract_id": contract.contract_id,
            "version": contract.version,
            "sha256": contract.descriptor_sha256,
        },
        "source": {
            "path": project_relative(source_path),
            "sha256": source_sha256,
            "row_count": source_row_count,
        },
        "mapping_report": {
            "path": project_relative(mapping_report_path),
            "content_sha256": mapping_report_sha256,
            "hash_mode": "content_sha256",
            # Backward-compatible field name; value is mapping report _run_info.content_sha256.
            "sha256": mapping_report_sha256,
        },
        "mapping_decisions": {
            "path": project_relative(decision_path),
            "sha256": decision_sha256,
        },
        "resources": [resource.to_dict() for resource in resources],
        "lineage": {
            "path": project_relative(lineage_path),
            "sha256": lineage_sha256,
            "entry_count": lineage_entry_count,
        },
        "build": {
            "read_only_source": True,
            "deterministic": True,
            "generated_resource_count": len(resources),
            "generated_row_count": sum(resource.row_count for resource in resources),
            "rejected_row_count": rejected_row_count,
        },
    }
    return attach_run_info(body)


def write_manifest(document: dict[str, Any], output_path: Path) -> None:
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
