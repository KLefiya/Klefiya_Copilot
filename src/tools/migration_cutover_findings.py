"""Normalize module one migration reports into cutover finding candidates.

The output is an intermediate, deterministic report. It is not wired into the
module three cutover plan, RAID register, activities, or gates yet.
"""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    from data_profile import attach_run_info
except ModuleNotFoundError:  # pragma: no cover - package import path.
    from src.tools.data_profile import attach_run_info

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_DIR = PROJECT_ROOT / "data" / "synthetic"

DEFAULT_VENDOR_VALIDATION_PATH = SYNTHETIC_DIR / "vendor_validation_report.json"
DEFAULT_VENDOR_DUPLICATE_PATH = SYNTHETIC_DIR / "vendor_duplicate_report.json"
DEFAULT_VENDOR_FIELD_MAPPING_PATH = SYNTHETIC_DIR / "vendor_field_mapping.json"
DEFAULT_GENERATED_VALIDATION_PATH = (
    SYNTHETIC_DIR / "erpnext_item_price_multitarget_generated_validation.json"
)
DEFAULT_OUTPUT_PATH = SYNTHETIC_DIR / "migration_cutover_findings.json"

REPORT_IDS = (
    "generated_validation",
    "vendor_duplicate",
    "vendor_field_mapping",
    "vendor_validation",
)

SEVERITIES = ("High", "Medium", "Low")
SEVERITY_RANK = {"High": 0, "Medium": 1, "Low": 2, None: 3}
RAID_TYPES = ("Dependency", "Issue", "Risk")
RECORD_SAMPLE_LIMIT = 10

VALIDATION_DEPENDENCY_ISSUES = {
    "mapping_needs_review",
    "no_target_in_schema",
    "target_not_creatable_or_updatable",
    "unmapped_target_key",
}
VALIDATION_RISK_ISSUES = {"possible_false_friend_target"}
VALIDATION_BLOCKING_ISSUES = {
    "duplicate_primary_key",
    "foreign_key",
    "max_length_overflow",
    "primary_key",
    "required",
    "required_field_missing",
    "type_not_parseable",
    "unique",
    "value_not_in_allowed_values",
}
NORMALIZATION_ROOT_CAUSES = {"max_length_overflow"}


class MigrationFindingError(ValueError):
    """Raised when a formal input report has an invalid shape."""


def project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MigrationFindingError(f"Required input is missing: {project_relative(path)}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise MigrationFindingError(f"Invalid JSON in {project_relative(path)}: {error}") from error
    if not isinstance(data, dict):
        raise MigrationFindingError(f"Report must be a JSON object: {project_relative(path)}")
    return data


def require_keys(report_id: str, report: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if key not in report:
            raise MigrationFindingError(f"{report_id} report is missing `{key}`.")


def require_array(report_id: str, report: dict[str, Any], key: str) -> list[Any]:
    value = report.get(key)
    if not isinstance(value, list):
        raise MigrationFindingError(f"{report_id} report field `{key}` must be an array.")
    return value


def source_sha(report_id: str, report: dict[str, Any]) -> str:
    sha = report.get("_run_info", {}).get("content_sha256")
    if not isinstance(sha, str) or not sha:
        raise MigrationFindingError(f"{report_id} report is missing `_run_info.content_sha256`.")
    return sha


def validate_inputs(reports: dict[str, dict[str, Any]]) -> None:
    missing = sorted(set(REPORT_IDS) - set(reports))
    if missing:
        raise MigrationFindingError(f"Missing formal input report(s): {', '.join(missing)}")

    require_keys("vendor_validation", reports["vendor_validation"], ("_run_info", "summary", "field_view", "record_view"))
    require_array("vendor_validation", reports["vendor_validation"], "field_view")
    require_array("vendor_validation", reports["vendor_validation"], "record_view")

    require_keys("vendor_duplicate", reports["vendor_duplicate"], ("_run_info", "summary", "duplicate_groups", "borderline_pairs"))
    require_array("vendor_duplicate", reports["vendor_duplicate"], "duplicate_groups")
    pairs = reports["vendor_duplicate"].get("borderline_pairs", {}).get("pairs")
    if pairs is not None and not isinstance(pairs, list):
        raise MigrationFindingError("vendor_duplicate report field `borderline_pairs.pairs` must be an array.")

    require_keys("vendor_field_mapping", reports["vendor_field_mapping"], ("_run_info", "mappings", "gaps"))
    require_array("vendor_field_mapping", reports["vendor_field_mapping"], "mappings")
    require_array("vendor_field_mapping", reports["vendor_field_mapping"], "gaps")

    require_keys("generated_validation", reports["generated_validation"], ("_run_info", "summary", "findings", "validation"))
    require_array("generated_validation", reports["generated_validation"], "findings")

    for report_id, report in reports.items():
        source_sha(report_id, report)


def normalize_severity(value: Any) -> tuple[str | None, str]:
    if not isinstance(value, str):
        return None, "none"
    normalized = value.strip().lower()
    mapped = {"high": "High", "medium": "Medium", "low": "Low"}.get(normalized)
    if mapped is None:
        return None, "none"
    return mapped, "source"


def rule_severity(value: str) -> tuple[str, str]:
    if value not in SEVERITIES:
        raise MigrationFindingError(f"Invalid rule severity: {value}")
    return value, "rule"


def choose_severity(current: str | None, incoming: str | None) -> str | None:
    return min((current, incoming), key=lambda item: SEVERITY_RANK[item])


def finding_id(rule_id: str, dedupe_key: str) -> str:
    seed = f"v1|{rule_id}|{dedupe_key}"
    return "MIG-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12].upper()


def stable_text(value: Any) -> str:
    if value is None:
        return "none"
    return str(value).strip()


def stable_token(value: Any) -> str:
    return stable_text(value).replace("\\", "/").lower()


def source_entry(
    *,
    report_id: str,
    content_sha256: str,
    json_pointer: str,
    matched_record_count: int = 0,
    record_ids_sample: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "report_id": report_id,
        "content_sha256": content_sha256,
        "json_pointer": json_pointer,
        "matched_record_count": matched_record_count,
        "record_ids_sample": sorted(record_ids_sample or [])[:RECORD_SAMPLE_LIMIT],
    }


def make_finding(
    *,
    rule_id: str,
    dedupe_key: str,
    category: str,
    title: str,
    description: str,
    affected_workstream: str,
    suggested_raid_type: str,
    severity: str | None,
    severity_origin: str,
    confidence: float | None,
    confidence_origin: str,
    review_required: bool,
    gate_blocker: bool,
    gate_reason: str | None,
    sources: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if suggested_raid_type not in RAID_TYPES:
        raise MigrationFindingError(f"Invalid suggested RAID type: {suggested_raid_type}")
    if gate_blocker and (review_required or severity != "High"):
        raise MigrationFindingError(f"Rule {rule_id} cannot create a blocker without explicit non-review High severity.")
    return {
        "finding_id": finding_id(rule_id, dedupe_key),
        "rule_id": rule_id,
        "dedupe_key": dedupe_key,
        "category": category,
        "title": title,
        "description": description,
        "affected_workstream": affected_workstream,
        "suggested_raid_type": suggested_raid_type,
        "severity": severity,
        "severity_origin": severity_origin,
        "confidence": confidence,
        "confidence_origin": confidence_origin,
        "status": "Open",
        "review_required": review_required,
        "gate_impact": {
            "blocker": gate_blocker,
            "reason": gate_reason if gate_blocker else None,
        },
        "sources": sorted(sources, key=lambda item: (item["report_id"], item["json_pointer"])),
        "evidence": evidence,
    }


def merge_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for finding in findings:
        key = (finding["rule_id"], finding["dedupe_key"])
        if key not in merged:
            merged[key] = deepcopy(finding)
            continue
        target = merged[key]
        target["sources"].extend(finding["sources"])
        unique_sources: dict[tuple[str, str], dict[str, Any]] = {}
        for source in target["sources"]:
            source_key = (source["report_id"], source["json_pointer"])
            if source_key not in unique_sources:
                unique_sources[source_key] = deepcopy(source)
            else:
                existing = unique_sources[source_key]
                existing["matched_record_count"] = max(
                    existing.get("matched_record_count", 0),
                    source.get("matched_record_count", 0),
                )
                existing["record_ids_sample"] = sorted(
                    set(existing.get("record_ids_sample", [])) | set(source.get("record_ids_sample", []))
                )[:RECORD_SAMPLE_LIMIT]
        target["sources"] = [unique_sources[key] for key in sorted(unique_sources)]
        target["severity"] = choose_severity(target["severity"], finding["severity"])
        if target["severity"] != finding["severity"] and finding["severity"] is not None:
            target["severity_origin"] = finding["severity_origin"]
        if target["confidence"] is None:
            target["confidence"] = finding["confidence"]
            target["confidence_origin"] = finding["confidence_origin"]
        target["review_required"] = target["review_required"] or finding["review_required"]
        target["gate_impact"]["blocker"] = target["gate_impact"]["blocker"] or finding["gate_impact"]["blocker"]
        if target["gate_impact"]["blocker"] and not target["gate_impact"]["reason"]:
            target["gate_impact"]["reason"] = finding["gate_impact"]["reason"]
        target["evidence"] = {**target["evidence"], **finding["evidence"]}
    return sorted(merged.values(), key=finding_sort_key)


def finding_sort_key(finding: dict[str, Any]) -> tuple[Any, ...]:
    return (
        finding["category"],
        finding["affected_workstream"],
        finding["suggested_raid_type"],
        SEVERITY_RANK[finding["severity"]],
        finding["dedupe_key"],
        finding["finding_id"],
    )


def validation_raid_type(issue_type: str) -> str:
    if issue_type in VALIDATION_DEPENDENCY_ISSUES:
        return "Dependency"
    if issue_type in VALIDATION_RISK_ISSUES:
        return "Risk"
    return "Issue"


def validation_review_required(issue_type: str, issue: dict[str, Any]) -> bool:
    return (
        issue_type in VALIDATION_DEPENDENCY_ISSUES
        or issue_type in VALIDATION_RISK_ISSUES
        or bool(issue.get("based_on_unverified"))
    )


def validation_root_causes(report: dict[str, Any]) -> set[tuple[str, str]]:
    causes: set[tuple[str, str]] = set()
    for entry in report["field_view"]:
        if not isinstance(entry, dict):
            raise MigrationFindingError("vendor_validation report field `field_view` entries must be objects.")
        legacy_field = stable_text(entry.get("legacy_field") or entry.get("field"))
        target = stable_text(entry.get("target"))
        for issue in entry.get("field_issues", []):
            if not isinstance(issue, dict):
                raise MigrationFindingError("vendor_validation report field `field_view.field_issues` entries must be objects.")
            if issue.get("issue_type") == "normalization_required":
                causes.add((stable_token(legacy_field or issue.get("field")), stable_token(target)))
    return causes


def build_validation_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    sha = source_sha("vendor_validation", report)
    findings: list[dict[str, Any]] = []
    normalization_causes = validation_root_causes(report)
    record_groups: dict[tuple[str, str, str], dict[str, Any]] = {}

    for entry in sorted(report["field_view"], key=lambda item: (stable_text(item.get("legacy_field")), stable_text(item.get("target")))):
        field_issues = entry.get("field_issues", [])
        if not isinstance(field_issues, list):
            raise MigrationFindingError("vendor_validation report field `field_view.field_issues` must be an array.")
        legacy_field = stable_text(entry.get("legacy_field") or "")
        target = stable_text(entry.get("target") or "")
        for issue in sorted(field_issues, key=lambda item: (stable_text(item.get("issue_type")), stable_text(item.get("field")))):
            issue_type = stable_text(issue.get("issue_type"))
            field = stable_text(issue.get("field") or legacy_field)
            issue_target = target
            severity, severity_origin = normalize_severity(issue.get("severity"))
            raid_type = validation_raid_type(issue_type)
            review_required = validation_review_required(issue_type, issue)
            dedupe_key = f"validation|{stable_token(field)}|{stable_token(issue_target)}|{stable_token(issue_type)}"
            gate_blocker = (
                severity == "High"
                and severity_origin == "source"
                and not review_required
                and issue_type in VALIDATION_BLOCKING_ISSUES
            )
            findings.append(
                make_finding(
                    rule_id="MIG-VAL-001",
                    dedupe_key=dedupe_key,
                    category="master_data_validation",
                    title=f"Migration validation issue for {field}",
                    description=stable_text(issue.get("detail_zh") or issue.get("message") or f"{issue_type} on {field}."),
                    affected_workstream="master_data",
                    suggested_raid_type=raid_type,
                    severity=severity,
                    severity_origin=severity_origin,
                    confidence=None,
                    confidence_origin="none",
                    review_required=review_required,
                    gate_blocker=gate_blocker,
                    gate_reason=f"High severity migration validation failure: {issue_type}." if gate_blocker else None,
                    sources=[
                        source_entry(
                            report_id="vendor_validation",
                            content_sha256=sha,
                            json_pointer=f"/field_view/field={stable_token(field)},target={stable_token(issue_target)}/field_issues/issue_type={stable_token(issue_type)}",
                        )
                    ],
                    evidence={
                        "issue_type": issue_type,
                        "field": field or None,
                        "target": issue_target or None,
                    },
                )
            )

    for record in sorted(report["record_view"], key=lambda item: stable_text(item.get("record_id"))):
        issues = record.get("issues", [])
        if not isinstance(issues, list):
            raise MigrationFindingError("vendor_validation report field `record_view.issues` must be an array.")
        record_id = stable_text(record.get("record_id"))
        for issue in sorted(issues, key=lambda item: (stable_text(item.get("field")), stable_text(item.get("target")), stable_text(item.get("issue_type")))):
            issue_type = stable_text(issue.get("issue_type"))
            field = stable_text(issue.get("field"))
            target = stable_text(issue.get("target"))
            root_cause = issue_type
            if issue_type in NORMALIZATION_ROOT_CAUSES and (stable_token(field), stable_token(target)) in normalization_causes:
                root_cause = "normalization_required"
            group_key = (stable_token(field), stable_token(target), stable_token(root_cause))
            group = record_groups.setdefault(
                group_key,
                {
                    "field": field,
                    "target": target,
                    "issue_type": issue_type,
                    "root_cause": root_cause,
                    "severity": None,
                    "severity_origin": "none",
                    "record_ids": set(),
                    "based_on_unverified": False,
                },
            )
            severity, severity_origin = normalize_severity(issue.get("severity"))
            group["severity"] = choose_severity(group["severity"], severity)
            if severity is not None:
                group["severity_origin"] = severity_origin
            group["record_ids"].add(record_id)
            group["based_on_unverified"] = group["based_on_unverified"] or bool(issue.get("based_on_unverified"))

    for (field_key, target_key, root_key), group in sorted(record_groups.items()):
        issue_type = group["root_cause"]
        record_ids = sorted(group["record_ids"])
        raid_type = validation_raid_type(issue_type)
        review_required = validation_review_required(issue_type, {"based_on_unverified": group["based_on_unverified"]})
        gate_blocker = (
            group["severity"] == "High"
            and group["severity_origin"] == "source"
            and not review_required
            and issue_type in VALIDATION_BLOCKING_ISSUES | {"normalization_required"}
        )
        findings.append(
            make_finding(
                rule_id="MIG-VAL-001",
                dedupe_key=f"validation|{field_key}|{target_key}|{root_key}",
                category="master_data_validation",
                title=f"Record-level migration validation failures for {group['field']}",
                description=(
                    f"{len(record_ids)} source record(s) have {group['issue_type']} for "
                    f"{group['field']} -> {group['target']}."
                ),
                affected_workstream="master_data",
                suggested_raid_type=raid_type,
                severity=group["severity"],
                severity_origin=group["severity_origin"],
                confidence=None,
                confidence_origin="none",
                review_required=review_required,
                gate_blocker=gate_blocker,
                gate_reason=f"High severity record-level migration validation failure: {issue_type}." if gate_blocker else None,
                sources=[
                    source_entry(
                        report_id="vendor_validation",
                        content_sha256=sha,
                        json_pointer=f"/record_view/issues[field={field_key},target={target_key},root_cause={root_key}]",
                        matched_record_count=len(record_ids),
                        record_ids_sample=record_ids,
                    )
                ],
                evidence={
                    "issue_type": group["issue_type"],
                    "root_cause": group["root_cause"],
                    "field": group["field"] or None,
                    "target": group["target"] or None,
                    "matched_record_count": len(record_ids),
                },
            )
        )
    return findings


def record_ids_from_group(group: dict[str, Any]) -> list[str]:
    records = group.get("records", [])
    if not isinstance(records, list):
        raise MigrationFindingError("vendor_duplicate report field `duplicate_groups.records` must be an array.")
    return sorted(stable_text(record.get("legacy_vendor_id")) for record in records if isinstance(record, dict))


def build_duplicate_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    sha = source_sha("vendor_duplicate", report)
    findings: list[dict[str, Any]] = []
    groups = sorted(report["duplicate_groups"], key=lambda item: stable_text(item.get("group_id")))
    review_groups = [group for group in groups if bool(group.get("needs_review"))]
    auto_groups = [group for group in groups if not bool(group.get("needs_review"))]

    if auto_groups:
        record_ids = sorted({record_id for group in auto_groups for record_id in record_ids_from_group(group)})
        severity, severity_origin = rule_severity("Medium")
        findings.append(
            make_finding(
                rule_id="MIG-DUP-001",
                dedupe_key="duplicate|groups|merge_approval_required",
                category="duplicate_supplier",
                title="Duplicate supplier resolution requires approval",
                description=f"{len(auto_groups)} high-confidence duplicate supplier group(s) require human merge approval.",
                affected_workstream="master_data",
                suggested_raid_type="Issue",
                severity=severity,
                severity_origin=severity_origin,
                confidence=None,
                confidence_origin="none",
                review_required=True,
                gate_blocker=False,
                gate_reason=None,
                sources=[
                    source_entry(
                        report_id="vendor_duplicate",
                        content_sha256=sha,
                        json_pointer="/duplicate_groups[needs_review=false]",
                        matched_record_count=len(record_ids),
                        record_ids_sample=record_ids,
                    )
                ],
                evidence={
                    "duplicate_group_count": len(auto_groups),
                    "records_in_groups": len(record_ids),
                },
            )
        )

    severity, severity_origin = rule_severity("Medium")
    for group in review_groups:
        group_id = stable_text(group.get("group_id"))
        record_ids = record_ids_from_group(group)
        confidence = group.get("confidence", {})
        min_probability = confidence.get("min_match_probability") if isinstance(confidence, dict) else None
        findings.append(
            make_finding(
                rule_id="MIG-DUP-002",
                dedupe_key=f"duplicate|group|{stable_token(group_id)}|needs_review",
                category="duplicate_supplier",
                title=f"Suspected duplicate supplier group requires review: {group_id}",
                description="Automated entity resolution flagged this duplicate group for human review before any merge action.",
                affected_workstream="master_data",
                suggested_raid_type="Risk",
                severity=severity,
                severity_origin=severity_origin,
                confidence=float(min_probability) if isinstance(min_probability, (int, float)) else None,
                confidence_origin="source" if isinstance(min_probability, (int, float)) else "none",
                review_required=True,
                gate_blocker=False,
                gate_reason=None,
                sources=[
                    source_entry(
                        report_id="vendor_duplicate",
                        content_sha256=sha,
                        json_pointer=f"/duplicate_groups/group_id={stable_token(group_id)}",
                        matched_record_count=len(record_ids),
                        record_ids_sample=record_ids,
                    )
                ],
                evidence={
                    "group_id": group_id,
                    "review_reasons": sorted(stable_text(reason) for reason in group.get("review_reasons", [])),
                    "min_match_probability": min_probability,
                },
            )
        )

    pairs = report.get("borderline_pairs", {}).get("pairs") or []
    if pairs:
        pair_ids = sorted({stable_text(record_id) for pair in pairs for record_id in pair.get("record_ids", [])})
        severity, severity_origin = rule_severity("Medium")
        findings.append(
            make_finding(
                rule_id="MIG-DUP-003",
                dedupe_key="duplicate|borderline_pairs|review_required",
                category="duplicate_supplier",
                title="Borderline duplicate supplier pairs require review",
                description=f"{len(pairs)} borderline supplier pair(s) were scored below the clustering threshold and require review.",
                affected_workstream="master_data",
                suggested_raid_type="Risk",
                severity=severity,
                severity_origin=severity_origin,
                confidence=None,
                confidence_origin="none",
                review_required=True,
                gate_blocker=False,
                gate_reason=None,
                sources=[
                    source_entry(
                        report_id="vendor_duplicate",
                        content_sha256=sha,
                        json_pointer="/borderline_pairs/pairs",
                        matched_record_count=len(pairs),
                        record_ids_sample=pair_ids,
                    )
                ],
                evidence={
                    "borderline_pair_count": len(pairs),
                    "threshold_used_for_clustering": report.get("borderline_pairs", {}).get("threshold_used_for_clustering"),
                },
            )
        )
    return findings


def build_mapping_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    sha = source_sha("vendor_field_mapping", report)
    findings: list[dict[str, Any]] = []

    mappings = sorted(report["mappings"], key=lambda item: stable_text(item.get("legacy_field") or item.get("source_field")))
    for mapping in mappings:
        status = stable_text(mapping.get("status"))
        needs_review = bool(mapping.get("needs_review")) or status == "needs_review"
        if not needs_review and status not in {"possible_false_friend", "no_confident_target"}:
            continue
        source_field = stable_text(mapping.get("legacy_field") or mapping.get("source_field"))
        confidence = mapping.get("confidence")
        if status == "no_confident_target":
            rule_id = "MIG-MAP-002"
            raid_type = "Dependency"
        else:
            rule_id = "MIG-MAP-001"
            raid_type = "Risk"
        severity, severity_origin = rule_severity("Medium")
        findings.append(
            make_finding(
                rule_id=rule_id,
                dedupe_key=f"mapping|{stable_token(source_field)}|{stable_token(status)}",
                category="field_mapping",
                title=f"Field mapping requires review: {source_field}",
                description=f"Mapping status for {source_field} is {status}; reviewer approval is required before cutover use.",
                affected_workstream="master_data",
                suggested_raid_type=raid_type,
                severity=severity,
                severity_origin=severity_origin,
                confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
                confidence_origin="source" if isinstance(confidence, (int, float)) else "none",
                review_required=True,
                gate_blocker=False,
                gate_reason=None,
                sources=[
                    source_entry(
                        report_id="vendor_field_mapping",
                        content_sha256=sha,
                        json_pointer=f"/mappings/source_field={stable_token(source_field)}",
                    )
                ],
                evidence={
                    "status": status,
                    "recommendation": mapping.get("recommendation"),
                    "band": mapping.get("band"),
                },
            )
        )

    gaps = sorted(report["gaps"], key=lambda item: (stable_text(item.get("legacy_field") or item.get("source_field")), stable_text(item.get("status"))))
    for gap in gaps:
        source_field = stable_text(gap.get("legacy_field") or gap.get("source_field"))
        status = stable_text(gap.get("status"))
        if status == "no_confident_target":
            rule_id = "MIG-MAP-002"
            raid_type = "Dependency"
        else:
            rule_id = "MIG-MAP-001"
            raid_type = "Risk"
        severity, severity_origin = rule_severity("Medium")
        confidence = gap.get("best_confidence")
        findings.append(
            make_finding(
                rule_id=rule_id,
                dedupe_key=f"mapping|{stable_token(source_field)}|{stable_token(status)}",
                category="field_mapping",
                title=f"Field mapping gap requires review: {source_field}",
                description=stable_text(gap.get("message") or f"Mapping gap for {source_field}."),
                affected_workstream="master_data",
                suggested_raid_type=raid_type,
                severity=severity,
                severity_origin=severity_origin,
                confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
                confidence_origin="source" if isinstance(confidence, (int, float)) else "none",
                review_required=True,
                gate_blocker=False,
                gate_reason=None,
                sources=[
                    source_entry(
                        report_id="vendor_field_mapping",
                        content_sha256=sha,
                        json_pointer=f"/gaps/source_field={stable_token(source_field)},status={stable_token(status)}",
                    )
                ],
                evidence={
                    "status": status,
                    "best_candidate": gap.get("best_candidate"),
                },
            )
        )
    return findings


def build_generated_validation_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    findings = report["findings"]
    summary = report.get("summary", {})
    if summary.get("valid") is True and not findings:
        return []

    sha = source_sha("generated_validation", report)
    rows: list[dict[str, Any]] = []
    for item in sorted(
        findings,
        key=lambda value: (
            stable_text(value.get("resource")),
            stable_text(value.get("row_number")),
            stable_text(value.get("field")),
            stable_text(value.get("category")),
            stable_text(value.get("raw_code")),
            stable_text(value.get("message")),
        ),
    ):
        category = stable_text(item.get("category") or item.get("raw_code") or "unknown")
        resource = stable_text(item.get("resource"))
        field = stable_text(item.get("field"))
        row_number = item.get("row_number")
        severity, severity_origin = normalize_severity(item.get("severity"))
        review_required = severity is None
        gate_blocker = (
            summary.get("valid") is False
            and severity == "High"
            and severity_origin == "source"
            and not review_required
        )
        rows.append(
            make_finding(
                rule_id="MIG-GEN-001",
                dedupe_key=f"generated_validation|{stable_token(resource)}|{stable_token(field)}|{stable_token(category)}|{stable_token(row_number)}",
                category="generated_package_validation",
                title=f"Generated migration package validation failed: {resource}.{field}",
                description=stable_text(item.get("message") or item.get("note") or f"{category} in generated package."),
                affected_workstream="master_data",
                suggested_raid_type="Issue",
                severity=severity,
                severity_origin=severity_origin,
                confidence=None,
                confidence_origin="none",
                review_required=review_required,
                gate_blocker=gate_blocker,
                gate_reason=f"High severity generated package validation failure: {category}." if gate_blocker else None,
                sources=[
                    source_entry(
                        report_id="generated_validation",
                        content_sha256=sha,
                        json_pointer=f"/findings/resource={stable_token(resource)},field={stable_token(field)},category={stable_token(category)},row={stable_token(row_number)}",
                        matched_record_count=1 if row_number is not None else 0,
                    )
                ],
                evidence={
                    "resource": resource or None,
                    "field": field or None,
                    "category": category,
                    "raw_code": item.get("raw_code"),
                    "row_number": row_number,
                },
            )
        )
    return rows


def summarize(findings: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, int] = {}
    by_severity = {"High": 0, "Medium": 0, "Low": 0, "null": 0}
    by_raid_type = {raid_type: 0 for raid_type in RAID_TYPES}
    for finding in findings:
        by_category[finding["category"]] = by_category.get(finding["category"], 0) + 1
        by_severity[finding["severity"] if finding["severity"] is not None else "null"] += 1
        by_raid_type[finding["suggested_raid_type"]] += 1
    return {
        "finding_count": len(findings),
        "by_category": {key: by_category[key] for key in sorted(by_category)},
        "by_severity": by_severity,
        "by_suggested_raid_type": by_raid_type,
        "review_required_count": sum(1 for finding in findings if finding["review_required"]),
        "gate_blocker_count": sum(1 for finding in findings if finding["gate_impact"]["blocker"]),
    }


def build_source_reports(reports: dict[str, dict[str, Any]], findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = {report_id: 0 for report_id in REPORT_IDS}
    for finding in findings:
        for report_id in {source["report_id"] for source in finding["sources"]}:
            counts[report_id] += 1
    paths = {
        "generated_validation": "data/synthetic/erpnext_item_price_multitarget_generated_validation.json",
        "vendor_duplicate": "data/synthetic/vendor_duplicate_report.json",
        "vendor_field_mapping": "data/synthetic/vendor_field_mapping.json",
        "vendor_validation": "data/synthetic/vendor_validation_report.json",
    }
    return [
        {
            "report_id": report_id,
            "path": paths[report_id],
            "content_sha256": source_sha(report_id, reports[report_id]),
            "finding_count": counts[report_id],
        }
        for report_id in REPORT_IDS
    ]


def build_migration_cutover_findings(reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    validate_inputs(reports)
    findings = merge_findings(
        [
            *build_validation_findings(reports["vendor_validation"]),
            *build_duplicate_findings(reports["vendor_duplicate"]),
            *build_mapping_findings(reports["vendor_field_mapping"]),
            *build_generated_validation_findings(reports["generated_validation"]),
        ]
    )
    body = {
        "_meta": {
            "report_type": "migration_cutover_findings",
            "schema_version": "1.0",
            "data_classification": "synthetic_demo",
            "component": "module_1_migration_risk_standardizer",
            "formal_input_boundary": sorted(REPORT_IDS),
            "excluded_inputs": [
                "erpnext_item_price_blind_evaluation.json",
                "vendor_duplicate_report.evaluation",
                "vendor_duplicate_report.duplicate_groups[*].ground_truth",
                "vendor_profile_report.fields",
                "vendor_profile_report.quality_flags",
                "ground_truth",
                "benchmark_accuracy_precision_recall_f1",
            ],
            "record_ids_sample_limit": RECORD_SAMPLE_LIMIT,
            "id_rule": "MIG- + uppercase(first 12 hex chars of sha256('v1|' + rule_id + '|' + dedupe_key))",
            "gate_recommendation_boundary": (
                "Only explicit non-review High validation failures from formal inputs can recommend blocker=true."
            ),
        },
        "source_reports": build_source_reports(reports, findings),
        "summary": summarize(findings),
        "findings": findings,
    }
    return attach_run_info(body)


def load_default_reports(
    *,
    vendor_validation_path: Path = DEFAULT_VENDOR_VALIDATION_PATH,
    vendor_duplicate_path: Path = DEFAULT_VENDOR_DUPLICATE_PATH,
    vendor_field_mapping_path: Path = DEFAULT_VENDOR_FIELD_MAPPING_PATH,
    generated_validation_path: Path = DEFAULT_GENERATED_VALIDATION_PATH,
) -> dict[str, dict[str, Any]]:
    return {
        "vendor_validation": load_json(vendor_validation_path),
        "vendor_duplicate": load_json(vendor_duplicate_path),
        "vendor_field_mapping": load_json(vendor_field_mapping_path),
        "generated_validation": load_json(generated_validation_path),
    }


def write_report(report: dict[str, Any], output_path: Path = DEFAULT_OUTPUT_PATH) -> dict[str, Any]:
    output_abs = Path(output_path).resolve()
    try:
        output_abs.relative_to(PROJECT_ROOT)
    except ValueError as error:
        raise MigrationFindingError("Output path must be inside the project root") from error

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
    output_abs.write_text(json.dumps(next_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return next_report
