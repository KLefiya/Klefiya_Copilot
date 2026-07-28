"""Build a deterministic cutover plan and RAID register from module two output.

This tool intentionally does not call an LLM, network service, SAP API, or any
ground-truth/evaluation files. It consumes the public module two Fit/Gap report
and a synthetic cutover constraint file, then writes a reproducible module three
starter report.

Usage:
    python src/tools/build_cutover_plan.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from data_profile import attach_run_info  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_DIR = PROJECT_ROOT / "data" / "synthetic"
SOURCE_REPORT_PATH = SYNTHETIC_DIR / "gap_analysis_report.json"
CONSTRAINTS_PATH = SYNTHETIC_DIR / "cutover_constraints.json"
OUTPUT_PATH = SYNTHETIC_DIR / "cutover_plan_report.json"

SOURCE_REPORT_REL = "data/synthetic/gap_analysis_report.json"
CONSTRAINTS_REL = "data/synthetic/cutover_constraints.json"

PHASES = ("DESIGN", "BUILD", "TEST", "DEPLOY")
DOMAIN_OWNER_ROLES = {
    "P2P": "Business Process Owner",
    "O2C": "Business Process Owner",
    "R2R": "Finance Lead",
    "master_data": "Data Migration Lead",
}
PHASE_TIMING = {
    "DESIGN": ("T-30", "T-25"),
    "BUILD": ("T-24", "T-15"),
    "TEST": ("T-14", "T-7"),
    "DEPLOY": ("T-1", "T0"),
}
INTEGRATION_KEYWORDS = (
    "api",
    "archive",
    "endpoint",
    "external",
    "feed",
    "file",
    "interface",
    "layout",
    "ledger",
    "legacy",
    "load",
    "mapping",
    "nightly",
    "portal",
    "schedule",
    "send",
    "spreadsheet",
    "synchronization",
    "upload",
)
ALLOWED_RAID_TYPES = {"Risk", "Assumption", "Issue", "Dependency"}
OFFSET_RE = re.compile(r"^T(?:(?P<sign>[+-])(?P<num>\d+)|0)$")


class CutoverBuildError(RuntimeError):
    """Raised when input data is missing or structurally invalid."""


def load_json(path: Path) -> Any:
    if not path.is_file():
        try:
            display_path = path.relative_to(PROJECT_ROOT)
        except ValueError:
            display_path = path
        raise CutoverBuildError(f"Required input is missing: {display_path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CutoverBuildError(f"Invalid JSON in {path.relative_to(PROJECT_ROOT)}: {error}") from error


def require_gap_report_shape(report: dict[str, Any]) -> None:
    if not isinstance(report, dict):
        raise CutoverBuildError("Module two report must be a JSON object.")
    for key in ("_run_info", "_meta", "requirements", "dev_backlog"):
        if key not in report:
            raise CutoverBuildError(f"Module two report is missing `{key}`.")
    if not isinstance(report["requirements"], list) or not isinstance(report["dev_backlog"], list):
        raise CutoverBuildError("Module two report fields `requirements` and `dev_backlog` must be arrays.")
    content_sha = report.get("_run_info", {}).get("content_sha256")
    if not isinstance(content_sha, str) or not content_sha:
        raise CutoverBuildError("Module two report is missing `_run_info.content_sha256`.")
    for item in report["dev_backlog"]:
        for key in ("requirement_id", "source_note_id", "description", "domain", "rationale", "evidence", "confidence"):
            if key not in item:
                raise CutoverBuildError(f"Development backlog item is missing `{key}`.")


def slug_id(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().upper()).strip("-")
    if not slug:
        raise CutoverBuildError("Cannot derive a stable id from an empty requirement id.")
    return slug


def offset_value(offset: str) -> int | None:
    match = OFFSET_RE.match(offset)
    if not match:
        return None
    if offset == "T0":
        return 0
    value = int(match.group("num"))
    return value if match.group("sign") == "+" else -value


def detect_delivery_owner(item: dict[str, Any]) -> tuple[str, list[str]]:
    text = f"{item.get('description', '')} {item.get('rationale', '')}".lower()
    matched = sorted({keyword for keyword in INTEGRATION_KEYWORDS if keyword in text})
    if matched:
        return "Integration Lead", matched
    return "Technical Lead", []


def domain_owner(domain: str) -> str:
    return DOMAIN_OWNER_ROLES.get(domain, "Business Process Owner")


def activity(
    *,
    activity_id: str,
    work_package_id: str | None,
    title: str,
    description: str,
    workstream: str,
    owner_role: str,
    start_offset: str,
    end_offset: str,
    depends_on: list[str],
    source_requirement_id: str | None,
    source_note_id: str | None,
    source_domain: str | None,
    source_evidence: list[str],
    source_rationale: str,
    approval_gate: str | None,
    rollback_required: bool,
    rollback_action: str,
    milestone_id: str | None = None,
) -> dict[str, Any]:
    return {
        "activity_id": activity_id,
        "work_package_id": work_package_id,
        "title": title,
        "description": description,
        "workstream": workstream,
        "owner_role": owner_role,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "depends_on": sorted(depends_on),
        "source_requirement_id": source_requirement_id,
        "source_note_id": source_note_id,
        "source_domain": source_domain,
        "source_evidence": source_evidence,
        "source_rationale": source_rationale,
        "approval_gate": approval_gate,
        "rollback_required": rollback_required,
        "rollback_action": rollback_action,
        "status": "Not Started",
        "milestone_id": milestone_id,
    }


def build_work_packages(dev_backlog: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]], dict[str, list[str]]]:
    work_packages: list[dict[str, Any]] = []
    activities: list[dict[str, Any]] = []
    phase_ids: dict[str, list[str]] = {phase: [] for phase in PHASES}
    owner_keyword_hits: dict[str, list[str]] = {}

    for item in sorted(dev_backlog, key=lambda x: slug_id(x["requirement_id"])):
        requirement_slug = slug_id(item["requirement_id"])
        work_package_id = f"WP-{requirement_slug}"
        delivery_owner, keyword_hits = detect_delivery_owner(item)
        owner_keyword_hits[item["requirement_id"]] = keyword_hits
        business_owner = domain_owner(item["domain"])

        work_packages.append({
            "work_package_id": work_package_id,
            "source_requirement_id": item["requirement_id"],
            "source_note_id": item["source_note_id"],
            "source_domain": item["domain"],
            "title": f"Deliver development requirement {item['requirement_id']}",
            "description": item["description"],
            "owner_role": delivery_owner,
            "business_owner_role": business_owner,
            "status": "Not Started",
        })

        previous_activity_id: str | None = None
        for phase in PHASES:
            activity_id = f"ACT-{requirement_slug}-{phase}"
            start_offset, end_offset = PHASE_TIMING[phase]
            phase_owner = delivery_owner if phase in {"BUILD", "DEPLOY"} else business_owner
            depends_on = [previous_activity_id] if previous_activity_id else []
            approval_gate = {
                "DESIGN": "GATE-DESIGN-SIGNOFF",
                "TEST": "GATE-CUTOVER-READINESS",
                "DEPLOY": "GATE-GO-NOGO",
            }.get(phase)
            rollback_required = phase == "DEPLOY"
            rollback_action = (
                f"Back out deployment for {item['requirement_id']} and restore the last validated Day-1 configuration or interface package."
                if rollback_required else ""
            )

            entry = activity(
                activity_id=activity_id,
                work_package_id=work_package_id,
                title=f"{phase.title()} {item['requirement_id']}",
                description=f"{phase.title()} activity for: {item['description']}",
                workstream=item["domain"],
                owner_role=phase_owner,
                start_offset=start_offset,
                end_offset=end_offset,
                depends_on=depends_on,
                source_requirement_id=item["requirement_id"],
                source_note_id=item["source_note_id"],
                source_domain=item["domain"],
                source_evidence=sorted(item["evidence"]),
                source_rationale=item["rationale"],
                approval_gate=approval_gate,
                rollback_required=rollback_required,
                rollback_action=rollback_action,
            )
            activities.append(entry)
            phase_ids[phase].append(activity_id)
            previous_activity_id = activity_id

    return work_packages, activities, phase_ids, owner_keyword_hits


def build_shared_activities(phase_ids: dict[str, list[str]]) -> list[dict[str, Any]]:
    specs = [
        ("CUT-SCOPE-BASELINE", "Cutover scope baseline", "Baseline cutover scope after development designs are complete.", "T-30", "T-30", "Cutover Manager", phase_ids["DESIGN"], "GATE-DESIGN-SIGNOFF", "MS-CUT-SCOPE-BASELINE"),
        ("CUT-MOCK-EXECUTION", "Mock cutover", "Execute a synthetic mock cutover for the development backlog scope.", "T-21", "T-21", "Cutover Manager", ["CUT-SCOPE-BASELINE"], None, "MS-MOCK-CUTOVER"),
        ("CUT-INTEGRATION-READINESS", "Integration readiness review", "Confirm builds and interface/configuration readiness before final testing.", "T-14", "T-14", "Integration Lead", [*phase_ids["BUILD"], "CUT-MOCK-EXECUTION"], None, "MS-INTEGRATION-READINESS"),
        ("CUT-CUTOVER-READINESS", "Cutover readiness review", "Confirm testing is complete and the cutover plan can enter freeze controls.", "T-7", "T-7", "Cutover Manager", [*phase_ids["TEST"], "CUT-INTEGRATION-READINESS"], "GATE-CUTOVER-READINESS", "MS-CUTOVER-READINESS"),
        ("CUT-CODE-FREEZE", "Code freeze", "Activate code freeze for the approved cutover scope.", "T-5", "T-5", "Technical Lead", ["CUT-CUTOVER-READINESS"], None, "MS-CODE-FREEZE"),
        ("CUT-DATA-FREEZE", "Data freeze", "Activate data freeze for final Day-1 execution.", "T-2", "T-2", "Data Migration Lead", ["CUT-CODE-FREEZE"], None, "MS-DATA-FREEZE"),
        ("CUT-GO-NOGO", "Go / No-Go decision", "Approve or stop Day-1 execution based on readiness, freeze controls, and open RAID items.", "T-1", "T-1", "Cutover Manager", ["CUT-DATA-FREEZE"], "GATE-GO-NOGO", "MS-GO-NOGO"),
        ("CUT-DAY1-VALIDATION", "Day-1 execution and validation", "Validate all development deployments after Day-1 execution.", "T0", "T0", "Cutover Manager", ["CUT-GO-NOGO", *phase_ids["DEPLOY"]], None, "MS-DAY1"),
        ("CUT-RECONCILIATION", "Financial and interface reconciliation", "Reconcile finance-critical postings and interface handoffs after Day-1 validation.", "T+1", "T+1", "Finance Lead", ["CUT-DAY1-VALIDATION"], None, "MS-RECONCILIATION"),
        ("CUT-HYPERCARE-HANDOVER", "Hypercare handover", "Transfer remaining support items and ownership into hypercare.", "T+5", "T+5", "Cutover Manager", ["CUT-RECONCILIATION"], "GATE-HYPERCARE-HANDOVER", "MS-HYPERCARE-HANDOVER"),
    ]
    return [
        activity(
            activity_id=activity_id,
            work_package_id=None,
            title=title,
            description=description,
            workstream="shared_cutover",
            owner_role=owner_role,
            start_offset=start_offset,
            end_offset=end_offset,
            depends_on=depends_on,
            source_requirement_id=None,
            source_note_id=None,
            source_domain=None,
            source_evidence=[],
            source_rationale="Shared deterministic cutover governance activity.",
            approval_gate=approval_gate,
            rollback_required=False,
            rollback_action="",
            milestone_id=milestone_id,
        )
        for activity_id, title, description, start_offset, end_offset, owner_role, depends_on, approval_gate, milestone_id in specs
    ]


def build_raid_register(
    dev_backlog: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in sorted(dev_backlog, key=lambda x: slug_id(x["requirement_id"])):
        requirement_slug = slug_id(item["requirement_id"])
        linked_activities = [f"ACT-{requirement_slug}-{phase}" for phase in PHASES]
        items.append({
            "raid_id": f"RAID-DEP-{requirement_slug}",
            "type": "Dependency",
            "title": f"Development delivery required before Go / No-Go for {item['requirement_id']}",
            "description": f"The development delivery for {item['description']} must be complete before Go / No-Go.",
            "owner_role": detect_delivery_owner(item)[0],
            "probability": "Certain",
            "impact": "High",
            "severity": "High",
            "status": "Open",
            "mitigation": "Track DESIGN, BUILD, TEST, and DEPLOY completion in the cutover plan before the Go / No-Go gate.",
            "trigger": "Any linked activity remains Not Started or incomplete at T-1.",
            "linked_requirement_ids": [item["requirement_id"]],
            "linked_activity_ids": linked_activities,
            "source": "development_backlog",
        })

    for requirement in sorted(
        [r for r in requirements if r.get("llm", {}).get("needs_review")],
        key=lambda r: slug_id(r["extracted_id"]),
    ):
        llm = requirement["llm"]
        confidence = float(llm.get("confidence", 0.0))
        severity = "High" if confidence < 0.5 else "Medium"
        reasons = "; ".join(llm.get("needs_review_reasons", [])) or "needs_review=true"
        evidence = ", ".join(llm.get("evidence", [])) or "no evidence entries"
        items.append({
            "raid_id": f"RAID-RISK-{slug_id(requirement['extracted_id'])}",
            "type": "Risk",
            "title": f"Fit/Gap judgement requires review for {requirement['extracted_id']}",
            "description": (
                f"needs_review reason(s): {reasons}. "
                f"confidence={confidence:.2f}. evidence={evidence}."
            ),
            "owner_role": domain_owner(requirement["domain"]),
            "probability": "Possible",
            "impact": "Medium",
            "severity": severity,
            "status": "Open",
            "mitigation": "Review the classification and confirm whether cutover scope or RAID treatment must change.",
            "trigger": "Business owner does not confirm the judgement before Cutover Readiness.",
            "linked_requirement_ids": [requirement["extracted_id"]],
            "linked_activity_ids": [],
            "source": "needs_review",
        })

    return sorted(items, key=lambda item: item["raid_id"])


def detect_cycle(activity_ids: set[str], edges: dict[str, list[str]]) -> bool:
    indegree = {activity_id: 0 for activity_id in activity_ids}
    children: dict[str, list[str]] = defaultdict(list)
    for activity_id, depends_on in edges.items():
        for dependency in depends_on:
            children[dependency].append(activity_id)
            indegree[activity_id] += 1
    queue = deque(sorted([activity_id for activity_id, degree in indegree.items() if degree == 0]))
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for child in sorted(children[node]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    return visited != len(activity_ids)


def validate_report(
    report: dict[str, Any],
    source_report: dict[str, Any],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    owner_roles = set(constraints.get("owner_roles", []))
    activities = report["activities"]
    work_packages = report["work_packages"]
    raid_register = report["raid_register"]
    dev_requirement_ids = {item["requirement_id"] for item in source_report["dev_backlog"]}
    needs_review_ids = {item["extracted_id"] for item in source_report["requirements"] if item.get("llm", {}).get("needs_review")}

    id_values: list[str] = []
    for collection, key in (
        (work_packages, "work_package_id"),
        (activities, "activity_id"),
        (raid_register, "raid_id"),
        (report["milestones"], "milestone_id"),
        (report["freeze_windows"], "freeze_id"),
        (report["approval_gates"], "gate_id"),
    ):
        id_values.extend(item[key] for item in collection)
    duplicates = sorted([item for item, count in Counter(id_values).items() if count > 1])
    if duplicates:
        errors.append(f"Duplicate IDs: {duplicates}")

    activity_ids = {item["activity_id"] for item in activities}
    missing_dependencies = sorted({
        dependency
        for item in activities
        for dependency in item["depends_on"]
        if dependency not in activity_ids
    })
    if missing_dependencies:
        errors.append(f"Dependencies reference unknown activity IDs: {missing_dependencies}")

    edges = {item["activity_id"]: item["depends_on"] for item in activities}
    dependency_graph_acyclic = not detect_cycle(activity_ids, edges)
    if not dependency_graph_acyclic:
        errors.append("Activity dependency graph contains a cycle.")

    package_by_requirement = {item["source_requirement_id"]: item for item in work_packages}
    uncovered_development_requirements = sorted(dev_requirement_ids - set(package_by_requirement))
    if uncovered_development_requirements:
        errors.append(f"Development requirements without work packages: {uncovered_development_requirements}")

    for package in work_packages:
        requirement_slug = slug_id(package["source_requirement_id"])
        expected = {f"ACT-{requirement_slug}-{phase}" for phase in PHASES}
        actual = {item["activity_id"] for item in activities if item["work_package_id"] == package["work_package_id"]}
        if expected != actual:
            errors.append(f"Work package {package['work_package_id']} does not have DESIGN/BUILD/TEST/DEPLOY.")

    deployments_without_rollback = sorted([
        item["activity_id"]
        for item in activities
        if item["activity_id"].endswith("-DEPLOY") and (
            not item["rollback_required"] or not item["rollback_action"].strip()
        )
    ])
    if deployments_without_rollback:
        errors.append(f"DEPLOY activities without rollback: {deployments_without_rollback}")

    dependency_ids = {item["linked_requirement_ids"][0] for item in raid_register if item["type"] == "Dependency"}
    missing_dependency_raid = sorted(dev_requirement_ids - dependency_ids)
    if missing_dependency_raid:
        errors.append(f"Development requirements without RAID Dependency: {missing_dependency_raid}")

    risk_ids = {item["linked_requirement_ids"][0] for item in raid_register if item["type"] == "Risk"}
    missing_risk_raid = sorted(needs_review_ids - risk_ids)
    if missing_risk_raid:
        errors.append(f"needs_review requirements without RAID Risk: {missing_risk_raid}")

    unknown_owner_roles = sorted({
        role
        for role in (
            [item["owner_role"] for item in activities]
            + [item["owner_role"] for item in work_packages]
            + [item["business_owner_role"] for item in work_packages]
            + [item["owner_role"] for item in report["freeze_windows"]]
            + [item["exception_approval_role"] for item in report["freeze_windows"]]
            + [role for gate in report["approval_gates"] for role in gate["approver_roles"]]
            + [item["owner_role"] for item in raid_register]
        )
        if role not in owner_roles
    })
    if unknown_owner_roles:
        errors.append(f"Unknown owner roles: {unknown_owner_roles}")

    invalid_offsets = sorted({
        offset
        for item in activities
        for offset in (item["start_offset"], item["end_offset"])
        if offset_value(offset) is None
    } | {
        offset
        for item in report["freeze_windows"]
        for offset in (item["start_offset"], item["end_offset"])
        if offset_value(offset) is None
    } | {
        item["due_offset"]
        for item in report["approval_gates"]
        if offset_value(item["due_offset"]) is None
    } | {
        item["offset"]
        for item in report["milestones"]
        if offset_value(item["offset"]) is None
    })
    if invalid_offsets:
        errors.append(f"Invalid offsets: {invalid_offsets}")

    late_offsets = sorted([
        item["activity_id"]
        for item in activities
        if offset_value(item["start_offset"]) is not None
        and offset_value(item["end_offset"]) is not None
        and offset_value(item["start_offset"]) > offset_value(item["end_offset"])
    ] + [
        item["freeze_id"]
        for item in report["freeze_windows"]
        if offset_value(item["start_offset"]) is not None
        and offset_value(item["end_offset"]) is not None
        and offset_value(item["start_offset"]) > offset_value(item["end_offset"])
    ])
    if late_offsets:
        errors.append(f"Start offset after end offset: {late_offsets}")

    if report["_meta"]["source_report_content_sha256"] != source_report["_run_info"]["content_sha256"]:
        errors.append("Source report SHA does not match module two report _run_info.content_sha256.")

    invalid_raid_types = sorted({item["type"] for item in raid_register if item["type"] not in ALLOWED_RAID_TYPES})
    if invalid_raid_types:
        errors.append(f"Invalid RAID types: {invalid_raid_types}")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "dependency_graph_acyclic": dependency_graph_acyclic,
        "uncovered_development_requirements": uncovered_development_requirements,
        "deployments_without_rollback": deployments_without_rollback,
        "unknown_owner_roles": unknown_owner_roles,
        "missing_dependency_references": missing_dependencies,
    }


def build_cutover_plan(source_report: dict[str, Any], constraints: dict[str, Any]) -> dict[str, Any]:
    require_gap_report_shape(source_report)
    dev_backlog = sorted(source_report["dev_backlog"], key=lambda item: slug_id(item["requirement_id"]))
    requirements = sorted(source_report["requirements"], key=lambda item: slug_id(item["extracted_id"]))
    needs_review_count = sum(1 for item in requirements if item.get("llm", {}).get("needs_review"))
    work_packages, package_activities, phase_ids, owner_keyword_hits = build_work_packages(dev_backlog)
    shared_activities = build_shared_activities(phase_ids)
    activities = sorted(package_activities + shared_activities, key=lambda item: item["activity_id"])
    raid_register = build_raid_register(dev_backlog, requirements)

    report = {
        "_meta": {
            "module": "module_3_cutover_raid_governance",
            "component": "deterministic_cutover_plan_builder",
            "source_report": SOURCE_REPORT_REL,
            "constraints_file": CONSTRAINTS_REL,
            "source_report_content_sha256": source_report["_run_info"]["content_sha256"],
            "development_backlog_count": len(dev_backlog),
            "needs_review_count": needs_review_count,
            "work_package_count": len(work_packages),
            "activity_count": len(activities),
            "shared_activity_count": len(shared_activities),
            "raid_count": len(raid_register),
            "time_basis": constraints["_meta"]["time_basis"],
            "synthetic": constraints["_meta"]["synthetic"],
            "owner_role_rules": {
                "domain_owner_roles": DOMAIN_OWNER_ROLES,
                "development_delivery_default": "Technical Lead",
                "integration_delivery_owner": "Integration Lead",
                "integration_keyword_rule": {
                    "keywords": INTEGRATION_KEYWORDS,
                    "matched_by_requirement_id": owner_keyword_hits,
                },
            },
            "raid_severity_rules": {
                "needs_review_confidence_below_0_50": "High",
                "needs_review_confidence_0_50_to_below_0_70": "Medium",
                "other_needs_review_reasons": "Medium",
            },
        },
        "milestones": sorted(constraints["shared_milestones"], key=lambda item: (offset_value(item["offset"]), item["milestone_id"])),
        "freeze_windows": sorted(constraints["freeze_windows"], key=lambda item: item["freeze_id"]),
        "approval_gates": sorted(constraints["approval_gates"], key=lambda item: item["gate_id"]),
        "work_packages": work_packages,
        "activities": activities,
        "raid_register": raid_register,
        "validation": {},
    }
    report["validation"] = validate_report(report, source_report, constraints)
    return report


def write_report(report: dict[str, Any], output_path: Path = OUTPUT_PATH) -> dict[str, Any]:
    if not report["validation"]["valid"]:
        raise CutoverBuildError(
            "Cutover plan validation failed:\n"
            + "\n".join(f"- {error}" for error in report["validation"]["errors"])
        )
    report_with_run_info = attach_run_info(report)
    if output_path.is_file():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
            existing_run = existing.get("_run_info", {})
            new_run = report_with_run_info.get("_run_info", {})
            if (
                existing_run.get("content_sha256") == new_run.get("content_sha256")
                and existing_run.get("generated_at")
            ):
                report_with_run_info["_run_info"]["generated_at"] = existing_run["generated_at"]
        except json.JSONDecodeError:
            pass
    output_path.write_text(json.dumps(report_with_run_info, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_with_run_info


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        source_report = load_json(SOURCE_REPORT_PATH)
        constraints = load_json(CONSTRAINTS_PATH)
        report = build_cutover_plan(source_report, constraints)
        report = write_report(report)
    except CutoverBuildError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error

    print(f"Development backlog : {report['_meta']['development_backlog_count']}")
    print(f"Work packages       : {report['_meta']['work_package_count']}")
    print(f"Activities          : {report['_meta']['activity_count']}")
    print(f"RAID items          : {report['_meta']['raid_count']}")
    print("Validation          : valid" if report["validation"]["valid"] else "Validation          : invalid")
    print(f"Content             : sha256 {report['_run_info']['content_sha256'][:16]}")
    print(f"Wrote report        : {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
