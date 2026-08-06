from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.core.contracts.loader import (
    PROJECT_ROOT,
    ContractLoadError,
    load_migration_contract,
)
from src.core.mapping.target_index import build_target_field_index
from src.core.hashing import provenance_text_or_raw_sha256, raw_file_sha256
from src.core.package_generation import builder as package_builder
from src.core.package_generation.builder import (
    PackageBuildBlocked,
    PackageGenerationError,
    write_build_report,
)
from src.core.package_generation.decision_loader import (
    DecisionLoadError,
    load_mapping_decisions,
)


router = APIRouter(prefix="/api/migration", tags=["migration-workspace"])


@dataclass(frozen=True)
class WorkspaceSpec:
    workspace_id: str
    title: str
    description: str
    contract_path: Path
    contract_data_root: Path
    source_path: Path
    mapping_report_path: Path
    seed_decision_path: Path
    runtime_root: Path
    record_id_field: str


WORKSPACE = WorkspaceSpec(
    workspace_id="erpnext-item-price",
    title="ERPNext Item + Item Price",
    description="Human-approved ERPNext Item and Item Price mapping review workspace.",
    contract_path=PROJECT_ROOT / "contracts" / "erpnext_item_price_reference" / "datapackage.yaml",
    contract_data_root=PROJECT_ROOT / "data" / "examples" / "blind" / "erpnext_item_price",
    source_path=PROJECT_ROOT / "data" / "examples" / "blind" / "erpnext_item_price" / "source_product_catalog.csv",
    mapping_report_path=PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_mapping.json",
    seed_decision_path=PROJECT_ROOT / "data" / "examples" / "remediation" / "erpnext_item_price" / "mapping_decisions.yaml",
    runtime_root=PROJECT_ROOT / "data" / "runtime" / "migration_workspaces" / "erpnext-item-price",
    record_id_field="article_number",
)
WORKSPACES = {WORKSPACE.workspace_id: WORKSPACE}
BUILD_LOCKS = {WORKSPACE.workspace_id: threading.Lock()}


class TransformationPayload(BaseModel):
    type: Literal["copy", "constant", "value_map"] = "copy"
    value: str | None = None
    values: dict[str, str] | None = None
    on_missing: Literal["reject_row", "keep_original", "empty"] | None = None


class DecisionPayload(BaseModel):
    source_field: str
    target: str | None = None
    decision: Literal["approved", "rejected", "deferred"]
    reason: str | None = None
    transformation: TransformationPayload = Field(default_factory=TransformationPayload)


class SaveDecisionsPayload(BaseModel):
    expected_mapping_content_sha256: str
    expected_decision_sha256: str
    decisions: list[DecisionPayload]


class BuildPayload(BaseModel):
    expected_mapping_content_sha256: str
    expected_decision_sha256: str


def _raw_file_sha256(path: Path) -> str:
    return raw_file_sha256(path)


def _decision_state_sha256(path: Path) -> str:
    # Backward-compatible API field name; YAML decisions use normalized text SHA.
    return provenance_text_or_raw_sha256(path)


def _project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "malformed_runtime_artifact", "message": f"{_project_relative(path)} is not valid JSON."},
        ) from exc
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=500,
            detail={"error": "malformed_runtime_artifact", "message": f"{_project_relative(path)} must be a JSON object."},
        )
    return value


def _workspace_or_404(workspace_id: str) -> WorkspaceSpec:
    spec = WORKSPACES.get(workspace_id)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "unknown_workspace", "message": f"Unknown migration workspace `{workspace_id}`."},
        )
    return spec


def _ensure_runtime_path(spec: WorkspaceSpec, path: Path) -> Path:
    resolved = path.resolve()
    runtime = spec.runtime_root.resolve()
    try:
        resolved.relative_to(runtime)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "runtime_path_escape", "message": "Runtime path must stay inside the workspace runtime root."},
        ) from exc
    return resolved


def _runtime_decision_path(spec: WorkspaceSpec) -> Path:
    return spec.runtime_root / "mapping_decisions.yaml"


def _generated_root(spec: WorkspaceSpec) -> Path:
    return spec.runtime_root / "generated"


def _reports_root(spec: WorkspaceSpec) -> Path:
    return spec.runtime_root / "reports"


def _build_report_path(spec: WorkspaceSpec) -> Path:
    return _reports_root(spec) / "package_build_report.json"


def _validation_report_path(spec: WorkspaceSpec) -> Path:
    return _reports_root(spec) / "generated_validation.json"


def _effective_decision_path(spec: WorkspaceSpec) -> tuple[Path, str]:
    runtime = _runtime_decision_path(spec)
    if runtime.is_file():
        return runtime, "runtime"
    return spec.seed_decision_path, "seed"


def _load_contract(spec: WorkspaceSpec):
    return load_migration_contract(spec.contract_path, spec.contract_data_root)


def _mapping_report(spec: WorkspaceSpec) -> dict[str, Any]:
    return _json(spec.mapping_report_path)


def _mapping_content_sha(spec: WorkspaceSpec) -> str:
    report = _mapping_report(spec)
    run_info = report.get("_run_info", {})
    if isinstance(run_info, dict) and isinstance(run_info.get("content_sha256"), str):
        return str(run_info["content_sha256"])
    return _raw_file_sha256(spec.mapping_report_path)


def _source_shape(spec: WorkspaceSpec) -> tuple[list[str], int]:
    with spec.source_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = sum(1 for _ in reader)
    return fields, rows


def _resource_fields(contract: Any) -> dict[str, list[str]]:
    resources: dict[str, list[str]] = {}
    for resource in contract.descriptor.get("resources", []):
        fields = [str(field.get("name")) for field in resource.get("schema", {}).get("fields", [])]
        resources[str(resource.get("name"))] = fields
    return resources


def _target_resource(target: str | None) -> str | None:
    if not target or "." not in target:
        return None
    return target.split(".", 1)[0]


def _safe_mapping_item(item: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "source_field",
        "status",
        "recommendation",
        "confidence",
        "band",
        "mapping_basis",
        "source_profile",
        "review_reasons",
        "top_candidates",
    )
    cleaned = {key: item.get(key) for key in allowed if key in item}
    candidates = []
    for candidate in item.get("top_candidates", []):
        if not isinstance(candidate, dict):
            continue
        target = str(candidate.get("target", ""))
        candidates.append(
            {
                "target": target,
                "target_resource": _target_resource(target),
                "target_field": target.split(".", 1)[1] if "." in target else target,
                "rank": candidate.get("rank"),
                "score": candidate.get("score"),
                "semantic_score": candidate.get("semantic_score"),
                "fuzzy_score": candidate.get("fuzzy_score"),
                "alias_hit": candidate.get("alias_hit"),
                "alias_source": candidate.get("alias_source"),
                "lexical_overlap": candidate.get("lexical_overlap"),
                "type_gate": candidate.get("type_gate"),
                "warnings": candidate.get("warnings", []),
            }
        )
    cleaned["top_candidates"] = candidates
    return cleaned


def _decision_dicts(decisions: Any) -> list[dict[str, Any]]:
    return [item.to_dict() for item in decisions.decisions]


def _summary(contract: Any, decisions: Any) -> dict[str, int]:
    approved = decisions.approved()
    sources: dict[str, int] = {}
    for item in approved:
        sources[item.source_field] = sources.get(item.source_field, 0) + 1
    return {
        "source_rows": decisions.source_row_count,
        "source_fields": decisions.source_field_count,
        "target_fields": len(build_target_field_index(contract)),
        "approved_links": len(approved),
        "unique_approved_sources": len(sources),
        "rejected_sources": len(decisions.rejected()),
        "deferred_sources": len(decisions.deferred()),
        "multi_target_sources": sum(1 for count in sources.values() if count > 1),
    }


def _build_state(spec: WorkspaceSpec) -> dict[str, Any]:
    build_path = _build_report_path(spec)
    validation_path = _validation_report_path(spec)
    manifest_path = _generated_root(spec) / "package_manifest.json"
    if not build_path.is_file() or not validation_path.is_file() or not manifest_path.is_file():
        return {"available": False}
    build = _json(build_path)
    validation = _json(validation_path)
    manifest = _json(manifest_path)
    validation_summary = validation.get("summary", {})
    return {
        "available": True,
        "status": "completed",
        "summary": build.get("summary", {}),
        "validation": validation_summary,
        "manifest": {
            "content_sha256": manifest.get("_run_info", {}).get("content_sha256"),
            "resource_count": len(manifest.get("resources", [])),
        },
        "build_report_sha256": build.get("_run_info", {}).get("content_sha256"),
    }


def workspace_detail(spec: WorkspaceSpec) -> dict[str, Any]:
    try:
        contract = _load_contract(spec)
        decision_path, decision_source = _effective_decision_path(spec)
        decisions = load_mapping_decisions(decision_path, contract, spec.mapping_report_path)
    except (ContractLoadError, DecisionLoadError) as exc:
        code = getattr(exc, "code", "workspace_error")
        message = getattr(exc, "message", str(exc))
        raise HTTPException(status_code=422, detail={"error": code, "message": message}) from exc
    mapping = _mapping_report(spec)
    meta = mapping.get("_meta", {})
    return {
        "workspace": {
            "workspace_id": spec.workspace_id,
            "title": spec.title,
            "description": spec.description,
            "contract_id": contract.contract_id,
            "contract_version": contract.version,
            "contract_sha256": contract.descriptor_sha256,
            "domain": contract.domain,
            "source_path": _project_relative(spec.source_path),
            "source_sha256": decisions.source_sha256,
            "mapping_content_sha256": _mapping_content_sha(spec),
            "mapping_report_sha256": decisions.mapping_report_sha256,
            "decision_source": decision_source,
            "decision_sha256": decisions.decision_sha256,
            "runtime_state": decision_source == "runtime",
        },
        "summary": _summary(contract, decisions),
        "mappings": [_safe_mapping_item(item) for item in mapping.get("mappings", []) if isinstance(item, dict)],
        "decisions": _decision_dicts(decisions),
        "build": _build_state(spec),
        "resources": [{"name": name, "fields": fields} for name, fields in _resource_fields(contract).items()],
        "meta": {
            "mapping_summary": mapping.get("summary", {}),
            "target_field_count": meta.get("target_field_count"),
        },
    }


def _check_expected_state(spec: WorkspaceSpec, expected_mapping_sha: str, expected_decision_sha: str) -> None:
    if expected_mapping_sha != _mapping_content_sha(spec):
        raise HTTPException(
            status_code=409,
            detail={"error": "stale_mapping", "message": "Mapping report changed after the workspace was loaded."},
        )
    current_path, _ = _effective_decision_path(spec)
    current_sha = _decision_state_sha256(current_path)
    if expected_decision_sha != current_sha:
        raise HTTPException(
            status_code=409,
            detail={"error": "stale_decision", "message": "Decision state changed after the workspace was loaded."},
        )


def _decision_yaml(spec: WorkspaceSpec, payload: SaveDecisionsPayload) -> dict[str, Any]:
    return {
        "version": "1.1.0",
        "contract_id": "erpnext-item-price-reference-v1",
        "mapping_report": _project_relative(spec.mapping_report_path),
        "source": {
            "path": _project_relative(spec.source_path),
            "record_id_field": spec.record_id_field,
        },
        "decisions": [
            {
                "source_field": item.source_field,
                "target": item.target,
                "decision": item.decision,
                **({"reason": item.reason} if item.reason else {}),
                "transformation": item.transformation.model_dump(exclude_none=True),
            }
            for item in payload.decisions
        ],
    }


def _write_runtime_decision(spec: WorkspaceSpec, payload: SaveDecisionsPayload) -> None:
    contract = _load_contract(spec)
    runtime = _ensure_runtime_path(spec, _runtime_decision_path(spec))
    runtime.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".mapping_decisions.", suffix=".yaml", dir=runtime.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(yaml.safe_dump(_decision_yaml(spec, payload), sort_keys=False), encoding="utf-8")
        load_mapping_decisions(tmp, contract, spec.mapping_report_path)
        os.replace(tmp, runtime)
    except DecisionLoadError as exc:
        if tmp.exists():
            tmp.unlink()
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_decisions",
                "message": exc.message,
                "decision_error": exc.as_dict(),
            },
        ) from exc
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def _safe_runtime_output_root(spec: WorkspaceSpec, path: Path) -> Path:
    resolved = _ensure_runtime_path(spec, path)
    if resolved == spec.runtime_root.resolve():
        raise PackageGenerationError("output_root_too_broad", "Output root must be a workspace subdirectory", {})
    return resolved


def _build_package(spec: WorkspaceSpec) -> None:
    contract = _load_contract(spec)
    decision_path, _ = _effective_decision_path(spec)
    generated = _ensure_runtime_path(spec, _generated_root(spec))
    reports = _ensure_runtime_path(spec, _reports_root(spec))
    reports.mkdir(parents=True, exist_ok=True)
    old_safe_output_root = package_builder._safe_output_root
    package_builder._safe_output_root = lambda path: _safe_runtime_output_root(spec, Path(path))
    try:
        report = package_builder.build_migration_package(
            contract,
            spec.source_path,
            spec.mapping_report_path,
            decision_path,
            generated,
            validation_report_path=_validation_report_path(spec),
        )
        write_build_report(report, _build_report_path(spec))
    finally:
        package_builder._safe_output_root = old_safe_output_root


@router.get("/workspaces")
def list_workspaces() -> dict[str, Any]:
    workspaces = []
    for spec in WORKSPACES.values():
        contract = _load_contract(spec)
        fields, rows = _source_shape(spec)
        workspaces.append(
            {
                "workspace_id": spec.workspace_id,
                "title": spec.title,
                "domain": contract.domain,
                "contract_id": contract.contract_id,
                "source_rows": rows,
                "source_fields": len(fields),
                "target_fields": len(build_target_field_index(contract)),
                "runtime_state": _runtime_decision_path(spec).is_file(),
            }
        )
    return {"workspaces": workspaces}


@router.get("/workspaces/{workspace_id}")
def get_workspace(workspace_id: str) -> dict[str, Any]:
    return workspace_detail(_workspace_or_404(workspace_id))


@router.put("/workspaces/{workspace_id}/decisions")
def save_decisions(workspace_id: str, payload: SaveDecisionsPayload) -> dict[str, Any]:
    spec = _workspace_or_404(workspace_id)
    _check_expected_state(spec, payload.expected_mapping_content_sha256, payload.expected_decision_sha256)
    _write_runtime_decision(spec, payload)
    return workspace_detail(spec)


@router.post("/workspaces/{workspace_id}/build")
def build_package(workspace_id: str, payload: BuildPayload) -> dict[str, Any]:
    spec = _workspace_or_404(workspace_id)
    _check_expected_state(spec, payload.expected_mapping_content_sha256, payload.expected_decision_sha256)
    lock = BUILD_LOCKS[spec.workspace_id]
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail={"error": "build_in_progress", "message": "Workspace build is already running."})
    try:
        _build_package(spec)
    except PackageBuildBlocked as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": exc.message, "details": exc.details}) from exc
    except (ContractLoadError, DecisionLoadError, PackageGenerationError) as exc:
        code = getattr(exc, "code", "package_generation_error")
        message = getattr(exc, "message", str(exc))
        raise HTTPException(status_code=422, detail={"error": code, "message": message}) from exc
    finally:
        lock.release()
    return workspace_detail(spec)


@router.post("/workspaces/{workspace_id}/reset")
def reset_workspace(workspace_id: str) -> dict[str, Any]:
    spec = _workspace_or_404(workspace_id)
    runtime = _ensure_runtime_path(spec, spec.runtime_root)
    if runtime.exists():
        shutil.rmtree(runtime)
    return workspace_detail(spec)


@router.get("/workspaces/{workspace_id}/resources/{resource_name}")
def get_resource_preview(
    workspace_id: str,
    resource_name: str,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    spec = _workspace_or_404(workspace_id)
    contract = _load_contract(spec)
    resources = _resource_fields(contract)
    if resource_name not in resources:
        raise HTTPException(status_code=404, detail={"error": "unknown_resource", "message": f"Unknown resource `{resource_name}`."})
    csv_path = _ensure_runtime_path(spec, _generated_root(spec) / f"{resource_name}.csv")
    if not csv_path.is_file():
        return {"resource": resource_name, "available": False, "columns": [], "rows": [], "total_rows": 0, "returned_rows": 0, "content_sha256": None}
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        columns = list(reader.fieldnames or [])
    return {
        "resource": resource_name,
        "available": True,
        "columns": columns,
        "rows": rows[:limit],
        "total_rows": len(rows),
        "returned_rows": min(limit, len(rows)),
        "content_sha256": _raw_file_sha256(csv_path),
    }


@router.get("/workspaces/{workspace_id}/lineage")
def get_lineage(
    workspace_id: str,
    source_field: str | None = None,
    target_resource: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    spec = _workspace_or_404(workspace_id)
    lineage_path = _ensure_runtime_path(spec, _generated_root(spec) / "lineage.json")
    if not lineage_path.is_file():
        return {"available": False, "total_entries": 0, "matched_entries": 0, "returned_entries": 0, "entries": []}
    document = _json(lineage_path)
    entries = [entry for entry in document.get("entries", []) if isinstance(entry, dict)]
    matched = [
        entry
        for entry in entries
        if (source_field is None or entry.get("source_field") == source_field)
        and (target_resource is None or entry.get("target_resource") == target_resource)
    ]
    allowed = (
        "source_row_number",
        "source_record_id",
        "source_field",
        "source_value_sha256",
        "target_resource",
        "target_row_number",
        "target_field",
        "transformation_type",
        "status",
    )
    return {
        "available": True,
        "total_entries": len(entries),
        "matched_entries": len(matched),
        "returned_entries": min(limit, len(matched)),
        "entries": [{key: entry.get(key) for key in allowed} for entry in matched[:limit]],
    }
