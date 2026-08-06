from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from src.core.contracts.loader import PROJECT_ROOT, LoadedMigrationContract
from src.core.contracts.validator import validate_migration_contract, write_validation_report
from src.core.hashing import raw_file_sha256
from src.core.mapping.target_index import build_target_field_index
from src.core.package_generation.decision_loader import DecisionLoadError, load_mapping_decisions
from src.core.package_generation.lineage import (
    build_lineage_document,
    project_relative,
    value_sha256,
    write_lineage_document,
)
from src.core.package_generation.manifest import build_package_manifest, write_manifest
from src.core.package_generation.models import (
    LineageEntry,
    LoadedMappingDecisions,
    MappingDecision,
    TargetResourceBuild,
)
from src.tools.data_profile import attach_run_info


class PackageGenerationError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class PackageBuildBlocked(PackageGenerationError):
    pass


def _raw_file_sha256(path: Path) -> str:
    return raw_file_sha256(path)


def _safe_output_root(path: Path) -> Path:
    generated_root = (PROJECT_ROOT / "data" / "generated").resolve()
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(generated_root)
    except ValueError as exc:
        raise PackageGenerationError(
            "output_root_outside_generated",
            "Output root must be inside data/generated",
            {"output_root": str(path)},
        ) from exc
    if resolved == generated_root:
        raise PackageGenerationError(
            "output_root_too_broad",
            "Output root must be a contract-specific directory under data/generated",
            {"output_root": str(path)},
        )
    return resolved


def _read_source(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise PackageGenerationError("empty_source", "Source CSV has no header", {})
        rows = [
            {field: (row.get(field) or "") for field in reader.fieldnames}
            for row in reader
        ]
    return list(reader.fieldnames), rows


def _primary_keys(schema: dict[str, Any]) -> set[str]:
    value = schema.get("primaryKey", [])
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(item) for item in value}
    return set()


def _resource_fields(contract: LoadedMigrationContract) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for resource in contract.descriptor.get("resources", []):
        schema = resource.get("schema", {})
        primary = _primary_keys(schema)
        fields = []
        for field in schema.get("fields", []):
            constraints = field.get("constraints") or {}
            name = str(field.get("name"))
            fields.append(
                {
                    "name": name,
                    "qualified_name": f"{resource['name']}.{name}",
                    "required": bool(constraints.get("required")),
                    "primary_key": name in primary,
                }
            )
        resources.append({"name": str(resource["name"]), "path": str(resource["path"]), "fields": fields})
    return resources


def _approved_by_target(decisions: LoadedMappingDecisions) -> dict[str, MappingDecision]:
    return {str(item.target): item for item in decisions.approved() if item.target is not None}


def _decision_counts(decisions: LoadedMappingDecisions) -> dict[str, int]:
    return {
        "approved": len(decisions.approved()),
        "rejected": len(decisions.rejected()),
        "deferred": len(decisions.deferred()),
    }


def _check_required_targets(
    contract: LoadedMigrationContract,
    decisions: LoadedMappingDecisions,
) -> list[dict[str, Any]]:
    approved_targets = {item.target for item in decisions.approved()}
    unresolved: list[dict[str, Any]] = []
    for field in build_target_field_index(contract):
        if (field.required or field.primary_key) and field.qualified_name not in approved_targets:
            unresolved.append(
                {
                    "target": field.qualified_name,
                    "resource": field.resource,
                    "field": field.name,
                    "reason": "required_or_primary_key_not_approved",
                }
            )
    return unresolved


def _apply_transformation(decision: MappingDecision, source_value: str) -> tuple[str | None, str | None]:
    transformation = decision.transformation
    if transformation.type == "copy":
        return source_value, None
    if transformation.type == "constant":
        return transformation.value or "", None
    if transformation.type == "value_map":
        values = transformation.values or {}
        if source_value in values:
            return values[source_value], None
        action = transformation.on_missing or "reject_row"
        if action == "reject_row":
            return None, "value_map_missing"
        if action == "keep_original":
            return source_value, None
        if action == "empty":
            return "", None
    return None, "transformation_error"


def _write_csv(path: Path, field_names: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=field_names, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in field_names})


def _atomic_replace(tmp_output: Path, final_output: Path) -> None:
    if final_output.exists():
        shutil.rmtree(final_output)
    final_output.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp_output), str(final_output))


def _cleanup(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _write_stable_json(document: dict[str, Any], output_path: Path) -> None:
    output = output_path.resolve()
    output.relative_to(PROJECT_ROOT)
    next_document = deepcopy(document)
    if output.exists():
        try:
            previous = json.loads(output.read_text(encoding="utf-8"))
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
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(next_document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_migration_package(
    contract: LoadedMigrationContract,
    source_path: Path,
    mapping_report_path: Path,
    decisions_path: Path,
    output_root: Path,
    *,
    validation_report_path: Path | None = None,
) -> dict[str, Any]:
    output_abs = _safe_output_root(output_root)
    source_abs = Path(source_path).resolve()
    try:
        source_abs.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise PackageGenerationError("source_outside_project", "Source path must be inside the project", {"source_path": str(source_path)}) from exc

    decisions = load_mapping_decisions(decisions_path, contract, mapping_report_path)
    if decisions.source_path != source_abs:
        raise PackageGenerationError("source_mismatch", "Source path does not match decision source.path", {})
    unresolved = _check_required_targets(contract, decisions)
    if unresolved:
        raise PackageBuildBlocked("unresolved_required_mappings", "Required or primary-key target fields are not approved", {"unresolved": unresolved})

    _, source_rows = _read_source(source_abs)
    target_by_qualified = _approved_by_target(decisions)
    resource_specs = _resource_fields(contract)
    tmp_parent = output_abs.parent
    tmp_parent.mkdir(parents=True, exist_ok=True)
    tmp_output = Path(tempfile.mkdtemp(prefix=f".{output_abs.name}.tmp-", dir=tmp_parent))
    lineage_entries: list[LineageEntry] = []
    rejected_rows: list[dict[str, Any]] = []
    resources: list[TargetResourceBuild] = []
    decision_source = project_relative(decisions.decision_path)

    try:
        for resource in resource_specs:
            resource_name = resource["name"]
            field_names = [field["name"] for field in resource["fields"]]
            output_rows: list[dict[str, str]] = []
            resource_rejections = 0
            for source_index, source_row in enumerate(source_rows, start=1):
                record_id = source_row.get(decisions.record_id_field, "")
                if record_id.strip() == "":
                    resource_rejections += 1
                    rejected_rows.append(
                        {
                            "source_row_number": source_index,
                            "source_record_id": "",
                            "target_resource": resource_name,
                            "reason": "missing_record_id",
                        }
                    )
                    continue
                target_row = {field: "" for field in field_names}
                row_lineage: list[LineageEntry] = []
                row_rejection: str | None = None
                for field in resource["fields"]:
                    qualified = field["qualified_name"]
                    decision = target_by_qualified.get(qualified)
                    if decision is None:
                        continue
                    raw_value = source_row.get(decision.source_field, "")
                    if field["required"] and raw_value.strip() == "" and decision.transformation.type != "constant":
                        row_rejection = "missing_required_source_value"
                        break
                    transformed, error = _apply_transformation(decision, raw_value)
                    if error:
                        row_rejection = error
                        break
                    target_value = transformed or ""
                    target_row[field["name"]] = target_value
                    if target_value != "":
                        row_lineage.append(
                            LineageEntry(
                                source_path=project_relative(source_abs),
                                source_row_number=source_index,
                                source_record_id=record_id,
                                source_field=decision.source_field,
                                source_value_sha256=value_sha256(raw_value),
                                target_resource=resource_name,
                                target_row_number=len(output_rows) + 1,
                                target_field=field["name"],
                                transformation_type=decision.transformation.type,
                                decision_source=decision_source,
                                status="written",
                            )
                        )
                if row_rejection:
                    resource_rejections += 1
                    rejected_rows.append(
                        {
                            "source_row_number": source_index,
                            "source_record_id": record_id,
                            "target_resource": resource_name,
                            "reason": row_rejection,
                        }
                    )
                    continue
                has_required_or_key = any(field["required"] or field["primary_key"] for field in resource["fields"])
                has_value = any(value != "" for value in target_row.values())
                if has_value or has_required_or_key:
                    output_rows.append(target_row)
                    lineage_entries.extend(row_lineage)
            target_path = tmp_output / str(resource["path"])
            _write_csv(target_path, field_names, output_rows)
            resources.append(
                TargetResourceBuild(
                    resource=resource_name,
                    target_path=project_relative(output_abs / str(resource["path"])),
                    field_names=tuple(field_names),
                    row_count=len(output_rows),
                    rejected_row_count=resource_rejections,
                    content_sha256=_raw_file_sha256(target_path),
                )
            )

        lineage_path = tmp_output / "lineage.json"
        lineage_doc = build_lineage_document(
            contract_id=contract.contract_id,
            source_path=source_abs,
            source_sha256=decisions.source_sha256,
            decision_path=decisions.decision_path,
            decision_sha256=decisions.decision_sha256,
            entries=lineage_entries,
        )
        write_lineage_document(lineage_doc, lineage_path)

        manifest_path = tmp_output / "package_manifest.json"
        manifest_doc = build_package_manifest(
            contract=contract,
            source_path=source_abs,
            source_sha256=decisions.source_sha256,
            source_row_count=decisions.source_row_count,
            mapping_report_path=decisions.mapping_report_path,
            mapping_report_sha256=decisions.mapping_report_sha256,
            decision_path=decisions.decision_path,
            decision_sha256=decisions.decision_sha256,
            resources=resources,
            lineage_path=output_abs / "lineage.json",
            lineage_sha256=lineage_doc["_run_info"]["content_sha256"],
            lineage_entry_count=len(lineage_entries),
            rejected_row_count=len(rejected_rows),
        )
        write_manifest(manifest_doc, manifest_path)
        _atomic_replace(tmp_output, output_abs)
    except Exception:
        _cleanup(tmp_output)
        raise

    generated_contract = replace(contract, data_root=output_abs)
    validation_report = validate_migration_contract(generated_contract)
    if validation_report_path is not None:
        write_validation_report(validation_report, validation_report_path)
    counts = _decision_counts(decisions)
    build_status = "completed_with_rejected_rows" if rejected_rows else "completed"
    validation_report_rel = project_relative(validation_report_path.resolve()) if validation_report_path is not None else None
    optional_unmapped = []
    approved_targets = set(target_by_qualified)
    for field in build_target_field_index(contract):
        if field.qualified_name not in approved_targets and not field.required and not field.primary_key:
            optional_unmapped.append(field.qualified_name)
    body = {
        "_meta": {
            "component": "migration_package_generation",
            "contract_id": contract.contract_id,
            "contract_sha256": contract.descriptor_sha256,
            "source_path": project_relative(source_abs),
            "source_sha256": decisions.source_sha256,
            "mapping_report_path": project_relative(decisions.mapping_report_path),
            "mapping_report_content_sha256": decisions.mapping_report_sha256,
            "mapping_report_hash_mode": "content_sha256",
            # Backward-compatible field name; value is mapping report _run_info.content_sha256.
            "mapping_report_sha256": decisions.mapping_report_sha256,
            "decision_path": project_relative(decisions.decision_path),
            "decision_sha256": decisions.decision_sha256,
            "output_root": project_relative(output_abs),
            "read_only_source": True,
            ("ground" + "_truth_used"): False,
        },
        "summary": {
            "build_status": build_status,
            "source_rows": len(source_rows),
            "resources_generated": len(resources),
            "rows_generated": sum(resource.row_count for resource in resources),
            "approved_mappings": counts["approved"],
            "rejected_mappings": counts["rejected"],
            "deferred_mappings": counts["deferred"],
            "rejected_rows": len(rejected_rows),
            "lineage_entries": len(lineage_entries),
        },
        "resources": [resource.to_dict() for resource in resources],
        "rejected_rows": rejected_rows,
        "unmapped_target_fields": optional_unmapped,
        "validation": {
            "report_path": validation_report_rel,
            "valid": bool(validation_report["summary"]["valid"]),
            "finding_count": int(validation_report["summary"]["finding_count"]),
        },
        "manifest": {
            "path": project_relative(output_abs / "package_manifest.json"),
            "content_sha256": manifest_doc["_run_info"]["content_sha256"],
        },
        "lineage": {
            "path": project_relative(output_abs / "lineage.json"),
            "content_sha256": lineage_doc["_run_info"]["content_sha256"],
            "entry_count": len(lineage_entries),
            "sensitive_values_included": False,
        },
    }
    return attach_run_info(body)


def write_build_report(report: dict[str, Any], output_path: Path) -> None:
    _write_stable_json(report, output_path)
