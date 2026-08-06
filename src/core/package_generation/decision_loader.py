from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import yaml

from src.core.contracts.loader import PROJECT_ROOT, LoadedMigrationContract
from src.core.hashing import canonical_json_content_sha256, normalized_text_sha256, provenance_text_or_raw_sha256
from src.core.mapping.target_index import build_target_field_index
from src.core.package_generation.models import (
    LoadedMappingDecisions,
    MappingDecision,
    TransformationSpec,
)


DECISIONS = {"approved", "rejected", "deferred"}
TRANSFORMATIONS = {"copy", "constant", "value_map"}
VALUE_MAP_ON_MISSING = {"reject_row", "keep_original", "empty"}
SECRET_MARKERS = ("authorization", "api_key", "secret", "token", "bearer ", "sk-")
EVALUATION_ANSWER_MARKERS = ("ground" + "_truth", "ground" + "-truth")


class DecisionLoadError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def _source_text_sha256(path: Path) -> str:
    return normalized_text_sha256(path)


def _project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _is_url(value: str) -> bool:
    normalized = value.lower().replace("\\", "/")
    return normalized.startswith(("http:/", "https:/", "ftp:/", "s3:/"))


def _safe_project_path(
    value: str | Path,
    label: str,
    *,
    must_exist: bool = True,
    allow_absolute: bool = False,
) -> Path:
    text = str(value)
    if _is_url(text):
        raise DecisionLoadError("remote_path_not_allowed", f"{label} must be local", {"path": text})
    candidate = Path(text)
    if candidate.is_absolute() and not allow_absolute:
        raise DecisionLoadError("absolute_path_not_allowed", f"{label} must be project-relative", {"path": text})
    if ".." in candidate.parts:
        raise DecisionLoadError("path_escape_not_allowed", f"{label} must not use path escape", {"path": text})
    resolved = candidate.resolve() if candidate.is_absolute() else (PROJECT_ROOT / candidate).resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise DecisionLoadError("path_outside_project", f"{label} must be inside the project", {"path": text}) from exc
    if must_exist and not resolved.exists():
        raise DecisionLoadError("path_missing", f"{label} does not exist", {"path": text})
    return resolved


def _scan_strings(value: Any, trail: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _scan_strings(item, (*trail, str(key)))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_strings(item, (*trail, str(index)))
        return
    if not isinstance(value, str):
        return
    lowered = value.lower()
    field = ".".join(trail)
    if any(marker in lowered for marker in EVALUATION_ANSWER_MARKERS):
        raise DecisionLoadError("ground" + "_truth_reference_not_allowed", "Decision files must not reference evaluation answers", {"field": field})
    if _is_url(value):
        raise DecisionLoadError("remote_path_not_allowed", "Decision files must not reference remote paths", {"field": field})
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise DecisionLoadError("unsafe_path_not_allowed", "Decision files must not contain absolute paths or path escapes", {"field": field})
    if any(marker in lowered for marker in SECRET_MARKERS):
        raise DecisionLoadError("secret_like_value_not_allowed", "Decision files must not contain credential-like values", {"field": field})


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DecisionLoadError("decision_parse_error", "Decision file is not valid YAML", {"path": _project_relative(path)}) from exc
    if not isinstance(parsed, dict):
        raise DecisionLoadError("decision_schema_error", "Decision file must be a mapping", {"path": _project_relative(path)})
    _scan_strings(parsed)
    return parsed


def _load_mapping_report(path: Path) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DecisionLoadError("mapping_report_parse_error", "Mapping report is not valid JSON", {"path": _project_relative(path)}) from exc
    if not isinstance(report, dict):
        raise DecisionLoadError("mapping_report_schema_error", "Mapping report must be a JSON object", {"path": _project_relative(path)})
    return report


def _source_header(path: Path) -> tuple[list[str], int]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise DecisionLoadError("empty_source", "Source CSV is empty", {"path": _project_relative(path)}) from exc
        return header, sum(1 for _ in reader)


def _mapping_index(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mappings = report.get("mappings")
    if not isinstance(mappings, list):
        raise DecisionLoadError("mapping_report_schema_error", "Mapping report must contain mappings", {})
    index: dict[str, dict[str, Any]] = {}
    for item in mappings:
        if not isinstance(item, dict) or "source_field" not in item:
            raise DecisionLoadError("mapping_report_schema_error", "Each mapping item must contain source_field", {})
        index[str(item["source_field"])] = item
    return index


def _top3_targets(mapping_item: dict[str, Any]) -> set[str]:
    candidates = mapping_item.get("top_candidates")
    if not isinstance(candidates, list):
        return set()
    return {
        str(candidate.get("target"))
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("target") is not None
    }


def _transformation(raw: Any) -> TransformationSpec:
    if raw is None:
        raw = {"type": "copy"}
    if not isinstance(raw, dict):
        raise DecisionLoadError("transformation_schema_error", "Transformation must be a mapping", {})
    kind = str(raw.get("type", ""))
    if kind not in TRANSFORMATIONS:
        raise DecisionLoadError("unsupported_transformation", "Unsupported transformation type", {"type": kind})
    if kind == "constant":
        if "value" not in raw:
            raise DecisionLoadError("transformation_schema_error", "Constant transformation requires value", {})
        return TransformationSpec(type=kind, value=str(raw["value"]))
    if kind == "value_map":
        values = raw.get("values")
        if not isinstance(values, dict):
            raise DecisionLoadError("transformation_schema_error", "Value map transformation requires values", {})
        on_missing = str(raw.get("on_missing", "reject_row"))
        if on_missing not in VALUE_MAP_ON_MISSING:
            raise DecisionLoadError("unsupported_on_missing", "Unsupported value_map on_missing action", {"on_missing": on_missing})
        return TransformationSpec(
            type=kind,
            values={str(key): str(value) for key, value in values.items()},
            on_missing=on_missing,
        )
    return TransformationSpec(type=kind)


def _parse_decisions(raw_decisions: Any) -> tuple[MappingDecision, ...]:
    if not isinstance(raw_decisions, list) or not raw_decisions:
        raise DecisionLoadError("decision_schema_error", "Decision file must contain a non-empty decisions list", {})
    decisions: list[MappingDecision] = []
    for raw in raw_decisions:
        if not isinstance(raw, dict):
            raise DecisionLoadError("decision_schema_error", "Each decision must be a mapping", {})
        missing = [key for key in ("source_field", "target", "decision") if key not in raw]
        if missing:
            raise DecisionLoadError("decision_schema_error", "Decision is missing required fields", {"missing": missing})
        decision = str(raw["decision"])
        if decision not in DECISIONS:
            raise DecisionLoadError("unknown_decision", "Decision must be approved, rejected, or deferred", {"decision": decision})
        target = raw["target"]
        decisions.append(
            MappingDecision(
                source_field=str(raw["source_field"]),
                target=None if target is None else str(target),
                decision=decision,
                reason=None if raw.get("reason") is None else str(raw["reason"]),
                transformation=_transformation(raw.get("transformation")),
            )
        )
    return tuple(decisions)


def load_mapping_decisions(
    decision_path: Path,
    contract: LoadedMigrationContract,
    mapping_report_path: Path,
) -> LoadedMappingDecisions:
    decision_abs = _safe_project_path(decision_path, "decision_path", allow_absolute=True)
    raw = _load_yaml(decision_abs)
    for key in ("version", "contract_id", "mapping_report", "source", "decisions"):
        if key not in raw:
            raise DecisionLoadError("decision_schema_error", "Decision file is missing required top-level fields", {"missing": key})
    if str(raw["contract_id"]) != contract.contract_id:
        raise DecisionLoadError("contract_id_mismatch", "Decision contract_id does not match loaded contract", {})

    mapping_from_file = _safe_project_path(raw["mapping_report"], "mapping_report")
    mapping_expected = _safe_project_path(mapping_report_path, "mapping_report_path", allow_absolute=True)
    if mapping_from_file != mapping_expected:
        raise DecisionLoadError("mapping_report_mismatch", "Decision mapping_report does not match the requested mapping report", {})
    mapping_report = _load_mapping_report(mapping_expected)
    meta = mapping_report.get("_meta", {})
    if meta.get("contract_id") != contract.contract_id:
        raise DecisionLoadError("mapping_report_contract_mismatch", "Mapping report contract_id does not match loaded contract", {})
    if meta.get("contract_sha256") != contract.descriptor_sha256:
        raise DecisionLoadError("stale_contract_sha", "Mapping report contract SHA does not match loaded contract", {})

    source = raw["source"]
    if not isinstance(source, dict) or "path" not in source or "record_id_field" not in source:
        raise DecisionLoadError("decision_schema_error", "Decision source must contain path and record_id_field", {})
    source_path = _safe_project_path(source["path"], "source.path")
    # Backward-compatible JSON field name; value is normalized_text_sha256 for CSV text.
    source_sha = _source_text_sha256(source_path)
    if meta.get("source_sha256") != source_sha:
        raise DecisionLoadError("stale_source_sha", "Mapping report source SHA does not match decision source", {})
    header, row_count = _source_header(source_path)
    header_set = set(header)
    record_id_field = str(source["record_id_field"])
    if record_id_field not in header_set:
        raise DecisionLoadError("record_id_field_missing", "record_id_field must exist in source CSV", {"field": record_id_field})

    target_fields = {field.qualified_name for field in build_target_field_index(contract)}
    mapping_by_source = _mapping_index(mapping_report)
    decisions = _parse_decisions(raw["decisions"])
    approved_links: set[tuple[str, str]] = set()
    approved_targets: set[str] = set()
    source_decisions: dict[str, set[str]] = {}
    for item in decisions:
        if item.source_field not in header_set:
            raise DecisionLoadError("source_field_missing", "Decision source field does not exist in source CSV", {"source_field": item.source_field})
        if item.source_field not in mapping_by_source:
            raise DecisionLoadError("source_field_missing_in_mapping_report", "Decision source field does not exist in mapping report", {"source_field": item.source_field})
        if item.decision == "approved":
            if not item.target:
                raise DecisionLoadError("approved_target_required", "Approved decisions must include a target", {"source_field": item.source_field})
            if item.target not in target_fields:
                raise DecisionLoadError("unknown_target", "Approved target does not exist in the contract", {"target": item.target})
            existing = source_decisions.get(item.source_field, set())
            if existing - {"approved"}:
                raise DecisionLoadError("conflicting_source_decisions", "A source field cannot mix approved, rejected, and deferred decisions", {"source_field": item.source_field})
            link = (item.source_field, item.target)
            if link in approved_links:
                raise DecisionLoadError("duplicate_approved_link", "A source-to-target link cannot be approved more than once", {"source_field": item.source_field, "target": item.target})
            if item.target in approved_targets:
                raise DecisionLoadError("duplicate_approved_target", "A target field cannot be approved more than once", {"target": item.target})
            if item.target not in _top3_targets(mapping_by_source[item.source_field]):
                raise DecisionLoadError("target_not_in_top3", "Approved target must appear in the mapping report top-3 candidates", {"source_field": item.source_field, "target": item.target})
            approved_links.add(link)
            approved_targets.add(item.target)
            source_decisions.setdefault(item.source_field, set()).add("approved")
        elif item.target is not None:
            raise DecisionLoadError("target_not_allowed", "Rejected and deferred decisions must not include a target", {"source_field": item.source_field})
        else:
            existing = source_decisions.get(item.source_field, set())
            if "approved" in existing or (existing and item.decision not in existing):
                raise DecisionLoadError("conflicting_source_decisions", "A source field cannot mix approved, rejected, and deferred decisions", {"source_field": item.source_field})
            if item.decision in existing:
                raise DecisionLoadError("duplicate_nonapproved_source_decision", "A rejected or deferred source decision cannot be repeated", {"source_field": item.source_field, "decision": item.decision})
            source_decisions.setdefault(item.source_field, set()).add(item.decision)

    return LoadedMappingDecisions(
        version=str(raw["version"]),
        contract_id=contract.contract_id,
        mapping_report_path=mapping_expected,
        mapping_report_sha256=mapping_report_content_sha(mapping_report),
        source_path=source_path,
        source_sha256=source_sha,
        source_row_count=row_count,
        source_field_count=len(header),
        record_id_field=record_id_field,
        decisions=decisions,
        decision_path=decision_abs,
        decision_sha256=provenance_text_or_raw_sha256(decision_abs),
        mapping_report=mapping_report,
    )


def mapping_report_content_sha(mapping_report: dict[str, Any]) -> str:
    run_info = mapping_report.get("_run_info", {})
    if not isinstance(run_info, dict) or not isinstance(run_info.get("content_sha256"), str):
        raise DecisionLoadError("mapping_report_content_sha_missing", "Mapping report is missing _run_info.content_sha256", {})
    content_sha = str(run_info["content_sha256"])
    if content_sha != canonical_json_content_sha256(mapping_report):
        raise DecisionLoadError(
            "mapping_report_content_sha_mismatch",
            "Mapping report _run_info.content_sha256 does not match canonical JSON content",
            {},
        )
    return content_sha
