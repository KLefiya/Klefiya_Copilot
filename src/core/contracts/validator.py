from __future__ import annotations

import csv
import json
import os
from collections import Counter
from copy import deepcopy
from importlib.metadata import version
from pathlib import Path
from typing import Any

from frictionless import validate

from src.core.contracts.loader import (
    PROJECT_ROOT,
    ContractLoadError,
    LoadedMigrationContract,
    load_migration_contract,
)
from src.tools.data_profile import attach_run_info


class ContractValidationError(Exception):
    pass


def _project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _category(error: dict[str, Any]) -> str:
    raw_code = str(error.get("type") or error.get("code") or "unknown")
    note = str(error.get("note") or "")
    if raw_code == "constraint-error":
        if 'constraint "required"' in note:
            return "required"
        if 'constraint "maxLength"' in note:
            return "max_length"
        if 'constraint "pattern"' in note:
            return "pattern"
        if 'constraint "enum"' in note:
            return "enum"
        return "constraint"
    return {
        "unique-error": "unique",
        "primary-key": "primary_key",
        "foreign-key": "foreign_key",
        "type-error": "type",
        "schema-error": "schema",
        "scheme-error": "schema",
        "package-error": "schema",
        "resource-error": "schema",
        "field-error": "schema",
    }.get(raw_code, "other")


def _severity(category: str) -> str:
    if category in {"required", "primary_key", "foreign_key", "unique"}:
        return "high"
    if category in {"enum", "max_length", "pattern", "type", "constraint"}:
        return "medium"
    return "low"


def _field(error: dict[str, Any]) -> str | None:
    if error.get("fieldName"):
        return str(error["fieldName"])
    field_names = error.get("fieldNames")
    if isinstance(field_names, list) and field_names:
        return ",".join(str(item) for item in field_names)
    return None


def _cells(error: dict[str, Any]) -> list[Any]:
    if isinstance(error.get("fieldCells"), list):
        return error["fieldCells"]
    if "cell" in error:
        return [error["cell"]]
    if isinstance(error.get("cells"), list):
        return error["cells"]
    return []


def _finding(resource: str, error: dict[str, Any]) -> dict[str, Any]:
    category = _category(error)
    return {
        "resource": resource,
        "row_number": error.get("rowNumber"),
        "field": _field(error),
        "category": category,
        "raw_code": str(error.get("type") or error.get("code") or "unknown"),
        "severity": _severity(category),
        "message": str(error.get("message") or ""),
        "note": str(error.get("note") or ""),
        "cells": _cells(error),
    }


def _count_rows(csv_path: Path) -> int:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def _sort_counts(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def _stable_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        findings,
        key=lambda item: (
            item["resource"],
            item["row_number"] if item["row_number"] is not None else -1,
            item["field"] or "",
            item["category"],
            item["raw_code"],
            item["message"],
        ),
    )


def _resource_reports(
    contract: LoadedMigrationContract,
    tasks: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    task_by_name = {str(task.get("name")): task for task in tasks}
    finding_counts = Counter(str(item["resource"]) for item in findings)
    resources = []
    for resource in contract.descriptor["resources"]:
        name = str(resource["name"])
        path = contract.data_root / str(resource["path"])
        task = task_by_name.get(name, {})
        resources.append(
            {
                "name": name,
                "path": _project_relative(path),
                "row_count": _count_rows(path),
                "valid": bool(task.get("valid", False)),
                "finding_count": finding_counts[name],
            }
        )
    return resources


def validate_migration_contract(
    contract: LoadedMigrationContract,
) -> dict[str, Any]:
    report = validate(deepcopy(contract.descriptor), basepath=str(contract.data_root))
    descriptor = report.to_descriptor()
    tasks = descriptor.get("tasks", [])
    findings: list[dict[str, Any]] = []
    for error in descriptor.get("errors", []):
        findings.append(_finding("_package", error))
    for task in tasks:
        resource = str(task.get("name") or "_resource")
        for error in task.get("errors", []):
            findings.append(_finding(resource, error))
    findings = _stable_findings(findings)
    resources = _resource_reports(contract, tasks, findings)
    by_category = Counter(item["category"] for item in findings)
    by_resource = Counter(item["resource"] for item in findings)
    by_severity = Counter(item["severity"] for item in findings)
    body = {
        "_meta": {
            "component": "migration_contract_validation",
            "engine": "frictionless",
            "engine_version": version("frictionless"),
            "contract_id": contract.contract_id,
            "contract_name": contract.name,
            "contract_version": contract.version,
            "adapter": contract.adapter,
            "domain": contract.domain,
            "synthetic": contract.synthetic,
            "authoritative": contract.authoritative,
            "contract_path": _project_relative(contract.descriptor_path),
            "contract_sha256": contract.descriptor_sha256,
            "data_root": _project_relative(contract.data_root),
            "resource_count": len(resources),
            "row_count": sum(item["row_count"] for item in resources),
            "read_only": True,
        },
        "summary": {
            "valid": bool(report.valid),
            "finding_count": len(findings),
            "by_category": _sort_counts(by_category),
            "by_resource": _sort_counts(by_resource),
            "by_severity": _sort_counts(by_severity),
        },
        "resources": resources,
        "findings": findings,
        "validation": {"valid": bool(report.valid)},
    }
    return attach_run_info(body)


def load_and_validate(
    descriptor_path: Path,
    data_root: Path,
) -> dict[str, Any]:
    contract = load_migration_contract(descriptor_path, data_root)
    return validate_migration_contract(contract)


def write_validation_report(report: dict[str, Any], output_path: Path) -> None:
    output_abs = Path(output_path).resolve()
    try:
        output_abs.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ContractValidationError("Output path must be inside the project root") from exc
    next_report = deepcopy(report)
    if output_abs.exists():
        try:
            previous = json.loads(output_abs.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
        previous_run = previous.get("_run_info", {})
        next_run = next_report.get("_run_info", {})
        if (
            previous_run.get("content_sha256")
            and previous_run.get("content_sha256") == next_run.get("content_sha256")
            and previous_run.get("generated_at")
        ):
            next_report["_run_info"]["generated_at"] = previous_run["generated_at"]
    if os.environ.get("CARVEOPS_OMIT_TIMESTAMP") == "1":
        next_report.get("_run_info", {}).pop("generated_at", None)
    output_abs.parent.mkdir(parents=True, exist_ok=True)
    output_abs.write_text(
        json.dumps(next_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
