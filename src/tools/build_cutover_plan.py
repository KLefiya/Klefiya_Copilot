"""Build a deterministic cutover plan and RAID register from module two output.

This tool intentionally does not call an LLM, network service, SAP API, or any
ground-truth/evaluation files. It consumes the public module two Fit/Gap report
and a synthetic cutover constraint file, then writes a reproducible module three
starter report.

Usage:
    python src/tools/build_cutover_plan.py
"""

from __future__ import annotations

import argparse
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
ALLOWED_MIGRATION_RAID_TYPES = {"Risk", "Issue", "Dependency"}
ALLOWED_MIGRATION_SEVERITIES = {"High", "Medium", "Low", None}
MIGRATION_ACTIVITY_ORDER = (
    "CUT-MIG-DUPLICATE-RESOLUTION",
    "CUT-MIG-MAPPING-REVIEW",
    "CUT-MIG-TARGET-DEPENDENCY",
    "CUT-MIG-VALIDATION-REMEDIATION",
    "CUT-MIG-PACKAGE-VALIDATION",
)
MIGRATION_ACTIVITY_SPECS = {
    "CUT-MIG-DUPLICATE-RESOLUTION": {
        "title": "Resolve duplicate supplier migration findings",
        "description": "Review and approve supplier duplicate treatment before final data freeze.",
        "start_offset": "T-10",
        "end_offset": "T-7",
    },
    "CUT-MIG-MAPPING-REVIEW": {
        "title": "Review migration field mapping findings",
        "description": "Confirm field mapping decisions and unresolved mapping gaps before target remediation.",
        "start_offset": "T-14",
        "end_offset": "T-10",
    },
    "CUT-MIG-TARGET-DEPENDENCY": {
        "title": "Resolve migration target dependencies",
        "description": "Close target schema and load dependency findings required for cutover readiness.",
        "start_offset": "T-10",
        "end_offset": "T-7",
    },
    "CUT-MIG-VALIDATION-REMEDIATION": {
        "title": "Remediate migration validation findings",
        "description": "Complete data quality and load validation remediation before data freeze.",
        "start_offset": "T-7",
        "end_offset": "T-2",
    },
    "CUT-MIG-PACKAGE-VALIDATION": {
        "title": "Validate generated migration package findings",
        "description": "Resolve generated package validation findings before the final migration load.",
        "start_offset": "T-7",
        "end_offset": "T-2",
    },
}
OFFSET_RE = re.compile(r"^T(?:(?P<sign>[+-])(?P<num>\d+)|0)$")


class CutoverBuildError(ValueError):
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


def require_migration_findings_shape(report: dict[str, Any]) -> None:
    if not isinstance(report, dict):
        raise CutoverBuildError("Migration findings report must be a JSON object.")
    for key in ("_run_info", "_meta", "source_reports", "summary", "findings"):
        if key not in report:
            raise CutoverBuildError(f"Migration findings report is missing `{key}`.")
    content_sha = report.get("_run_info", {}).get("content_sha256")
    if not isinstance(content_sha, str) or not content_sha:
        raise CutoverBuildError("Migration findings report is missing `_run_info.content_sha256`.")
    if report.get("_meta", {}).get("report_type") != "migration_cutover_findings":
        raise CutoverBuildError("Migration findings report `_meta.report_type` must be `migration_cutover_findings`.")
    if not isinstance(report["source_reports"], list):
        raise CutoverBuildError("Migration findings report field `source_reports` must be an array.")
    if not isinstance(report["summary"], dict):
        raise CutoverBuildError("Migration findings report field `summary` must be an object.")
    if not isinstance(report["findings"], list):
        raise CutoverBuildError("Migration findings report field `findings` must be an array.")

    seen_finding_ids: set[str] = set()
    seen_dedupe_keys: set[str] = set()
    required_finding_keys = (
        "finding_id",
        "rule_id",
        "dedupe_key",
        "category",
        "title",
        "description",
        "affected_workstream",
        "suggested_raid_type",
        "severity",
        "severity_origin",
        "status",
        "review_required",
        "gate_impact",
        "sources",
    )
    for finding in report["findings"]:
        if not isinstance(finding, dict):
            raise CutoverBuildError("Migration finding entries must be JSON objects.")
        for key in required_finding_keys:
            if key not in finding:
                raise CutoverBuildError(f"Migration finding is missing `{key}`.")

        finding_id = finding["finding_id"]
        dedupe_key = finding["dedupe_key"]
        if not isinstance(finding_id, str) or not finding_id:
            raise CutoverBuildError("Migration finding `finding_id` must be a non-empty string.")
        if finding_id in seen_finding_ids:
            raise CutoverBuildError(f"Duplicate migration finding_id: {finding_id}")
        seen_finding_ids.add(finding_id)
        if not isinstance(dedupe_key, str) or not dedupe_key:
            raise CutoverBuildError(f"Migration finding {finding_id} has an invalid `dedupe_key`.")
        if dedupe_key in seen_dedupe_keys:
            raise CutoverBuildError(f"Duplicate migration dedupe_key: {dedupe_key}")
        seen_dedupe_keys.add(dedupe_key)

        raid_type = finding["suggested_raid_type"]
        if raid_type not in ALLOWED_MIGRATION_RAID_TYPES:
            raise CutoverBuildError(f"Migration finding {finding_id} has invalid suggested_raid_type: {raid_type}")
        severity = finding["severity"]
        if severity not in ALLOWED_MIGRATION_SEVERITIES:
            raise CutoverBuildError(f"Migration finding {finding_id} has invalid severity: {severity}")
        if not isinstance(finding["review_required"], bool):
            raise CutoverBuildError(f"Migration finding {finding_id} field `review_required` must be boolean.")
        gate_impact = finding["gate_impact"]
        if not isinstance(gate_impact, dict) or not isinstance(gate_impact.get("blocker"), bool):
            raise CutoverBuildError(f"Migration finding {finding_id} field `gate_impact.blocker` must be boolean.")
        if gate_impact["blocker"] and finding["review_required"]:
            raise CutoverBuildError(f"Migration finding {finding_id} cannot be both gate blocker and review_required.")
        if gate_impact["blocker"] and severity != "High":
            raise CutoverBuildError(f"Migration finding {finding_id} gate blocker must have High severity.")
        if not isinstance(finding["sources"], list):
            raise CutoverBuildError(f"Migration finding {finding_id} field `sources` must be an array.")


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
    status: str = "Not Started",
    source_finding_ids: list[str] | None = None,
    linked_finding_ids: list[str] | None = None,
    linked_raid_ids: list[str] | None = None,
) -> dict[str, Any]:
    entry = {
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
        "status": status,
        "milestone_id": milestone_id,
    }
    if source_finding_ids is not None:
        entry["source_finding_ids"] = sorted(source_finding_ids)
    if linked_finding_ids is not None:
        entry["linked_finding_ids"] = sorted(linked_finding_ids)
    if linked_raid_ids is not None:
        entry["linked_raid_ids"] = sorted(linked_raid_ids)
    return entry


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


def migration_activity_id_for_finding(finding: dict[str, Any]) -> str:
    category = finding["category"]
    raid_type = finding["suggested_raid_type"]
    rule_id = finding["rule_id"]
    if category == "duplicate_supplier":
        return "CUT-MIG-DUPLICATE-RESOLUTION"
    if category == "field_mapping":
        return "CUT-MIG-MAPPING-REVIEW"
    if category == "master_data_validation" and raid_type == "Dependency":
        return "CUT-MIG-TARGET-DEPENDENCY"
    if category == "master_data_validation":
        return "CUT-MIG-VALIDATION-REMEDIATION"
    if category in {"generated_package_validation", "package_validation"} or rule_id.startswith("MIG-GEN"):
        return "CUT-MIG-PACKAGE-VALIDATION"
    return "CUT-MIG-VALIDATION-REMEDIATION"


def migration_source_summary(finding: dict[str, Any]) -> dict[str, Any]:
    sources = finding.get("sources", [])
    return {
        "type": "migration_cutover_finding",
        "finding_id": finding["finding_id"],
        "rule_id": finding["rule_id"],
        "dedupe_key": finding["dedupe_key"],
        "source_report_ids": sorted({source["report_id"] for source in sources}),
        "source_pointers": sorted({source["json_pointer"] for source in sources}),
    }


def migration_raid_item(finding: dict[str, Any], linked_activity_id: str) -> dict[str, Any]:
    severity = finding["severity"]
    gate_blocker = finding["gate_impact"]["blocker"]
    review_note = " Review is required before the finding can be accepted as resolved." if finding["review_required"] else ""
    mitigation = (
        f"Resolve or formally accept migration finding {finding['finding_id']} before data freeze."
        f"{review_note}"
    )
    trigger = (
        f"Finding {finding['finding_id']} remains Open for Data Migration Lead review at the migration cutover checkpoint."
    )
    return {
        "raid_id": f"RAID-{finding['finding_id']}",
        "type": finding["suggested_raid_type"],
        "title": finding["title"],
        "description": finding["description"],
        "owner_role": "Data Migration Lead",
        "probability": None,
        "impact": severity,
        "severity": severity,
        "severity_origin": finding["severity_origin"],
        "status": "Open",
        "mitigation": mitigation,
        "trigger": trigger,
        "linked_requirement_ids": [],
        "linked_activity_ids": [linked_activity_id],
        "linked_finding_ids": [finding["finding_id"]],
        "review_required": finding["review_required"],
        "gate_blocker": gate_blocker,
        "source": migration_source_summary(finding),
    }


def build_migration_raid_register(migration_findings_report: dict[str, Any]) -> list[dict[str, Any]]:
    findings = sorted(migration_findings_report["findings"], key=lambda finding: finding["finding_id"])
    return sorted(
        [
            migration_raid_item(finding, migration_activity_id_for_finding(finding))
            for finding in findings
        ],
        key=lambda item: item["raid_id"],
    )


def migration_activity_dependencies(activity_id: str, existing_activity_ids: set[str]) -> list[str]:
    dependencies: dict[str, list[str]] = {
        "CUT-MIG-DUPLICATE-RESOLUTION": ["CUT-MIG-MAPPING-REVIEW"],
        "CUT-MIG-MAPPING-REVIEW": [],
        "CUT-MIG-TARGET-DEPENDENCY": ["CUT-MIG-MAPPING-REVIEW"],
        "CUT-MIG-VALIDATION-REMEDIATION": [
            "CUT-MIG-MAPPING-REVIEW",
            "CUT-MIG-TARGET-DEPENDENCY",
            "CUT-MIG-DUPLICATE-RESOLUTION",
        ],
        "CUT-MIG-PACKAGE-VALIDATION": ["CUT-MIG-VALIDATION-REMEDIATION"],
    }
    return sorted([dependency for dependency in dependencies[activity_id] if dependency in existing_activity_ids])


def build_migration_activities(raid_register: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raid in raid_register:
        activity_id = raid["linked_activity_ids"][0]
        grouped[activity_id].append(raid)

    existing_activity_ids = set(grouped)
    activities: list[dict[str, Any]] = []
    for activity_id in MIGRATION_ACTIVITY_ORDER:
        if activity_id not in grouped:
            continue
        raids = sorted(grouped[activity_id], key=lambda item: item["raid_id"])
        finding_ids = sorted({finding_id for raid in raids for finding_id in raid["linked_finding_ids"]})
        raid_ids = [raid["raid_id"] for raid in raids]
        spec = MIGRATION_ACTIVITY_SPECS[activity_id]
        activities.append(
            activity(
                activity_id=activity_id,
                work_package_id=None,
                title=spec["title"],
                description=f"{spec['description']} Covers {len(finding_ids)} migration finding(s).",
                workstream="master_data",
                owner_role="Data Migration Lead",
                start_offset=spec["start_offset"],
                end_offset=spec["end_offset"],
                depends_on=migration_activity_dependencies(activity_id, existing_activity_ids),
                source_requirement_id=None,
                source_note_id=None,
                source_domain="master_data",
                source_evidence=finding_ids,
                source_rationale="Aggregated from deterministic migration cutover findings.",
                approval_gate="GATE-CUTOVER-READINESS",
                rollback_required=False,
                rollback_action="",
                status="Planned",
                source_finding_ids=finding_ids,
                linked_finding_ids=finding_ids,
                linked_raid_ids=raid_ids,
            )
        )
    return activities


def migration_provenance(migration_findings_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_type": migration_findings_report["_meta"]["report_type"],
        "content_sha256": migration_findings_report["_run_info"]["content_sha256"],
        "finding_count": len(migration_findings_report["findings"]),
        "source_report_ids": sorted(
            source_report["report_id"]
            for source_report in migration_findings_report["source_reports"]
        ),
    }


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

    dependency_ids = {
        item["linked_requirement_ids"][0]
        for item in raid_register
        if item["type"] == "Dependency"
        and item.get("source") == "development_backlog"
        and item.get("linked_requirement_ids")
    }
    missing_dependency_raid = sorted(dev_requirement_ids - dependency_ids)
    if missing_dependency_raid:
        errors.append(f"Development requirements without RAID Dependency: {missing_dependency_raid}")

    risk_ids = {
        item["linked_requirement_ids"][0]
        for item in raid_register
        if item["type"] == "Risk"
        and item.get("source") == "needs_review"
        and item.get("linked_requirement_ids")
    }
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

    raid_activity_references = sorted({
        activity_id
        for item in raid_register
        for activity_id in item.get("linked_activity_ids", [])
        if activity_id not in activity_ids
    })
    if raid_activity_references:
        errors.append(f"RAID items reference unknown activity IDs: {raid_activity_references}")

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


def build_cutover_plan(
    source_report: dict[str, Any],
    constraints: dict[str, Any],
    migration_findings_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_gap_report_shape(source_report)
    if migration_findings_report is not None:
        require_migration_findings_shape(migration_findings_report)
    dev_backlog = sorted(source_report["dev_backlog"], key=lambda item: slug_id(item["requirement_id"]))
    requirements = sorted(source_report["requirements"], key=lambda item: slug_id(item["extracted_id"]))
    needs_review_count = sum(1 for item in requirements if item.get("llm", {}).get("needs_review"))
    work_packages, package_activities, phase_ids, owner_keyword_hits = build_work_packages(dev_backlog)
    shared_activities = build_shared_activities(phase_ids)
    raid_register = build_raid_register(dev_backlog, requirements)
    migration_raid_register: list[dict[str, Any]] = []
    migration_activities: list[dict[str, Any]] = []
    if migration_findings_report is not None:
        migration_raid_register = build_migration_raid_register(migration_findings_report)
        migration_activities = build_migration_activities(migration_raid_register)
        migration_activity_ids = [item["activity_id"] for item in migration_activities]
        for shared_activity in shared_activities:
            if shared_activity["activity_id"] == "CUT-DATA-FREEZE":
                shared_activity["depends_on"] = sorted(set(shared_activity["depends_on"]) | set(migration_activity_ids))
                break
    activities = sorted(package_activities + shared_activities + migration_activities, key=lambda item: item["activity_id"])
    raid_register = sorted(raid_register + migration_raid_register, key=lambda item: item["raid_id"])

    meta = {
        "module": "module_3_cutover_raid_governance",
        "component": "deterministic_cutover_plan_builder",
        "source_report": SOURCE_REPORT_REL,
        "constraints_file": CONSTRAINTS_REL,
        "source_report_content_sha256": source_report["_run_info"]["content_sha256"],
        "development_backlog_count": len(dev_backlog),
        "needs_review_count": needs_review_count,
        "work_package_count": len(work_packages),
        "activity_count": len(activities),
        "shared_activity_count": len(shared_activities) + len(migration_activities),
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
    }
    if migration_findings_report is not None:
        meta["migration_findings"] = migration_provenance(migration_findings_report)

    report = {
        "_meta": {
            **meta,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the deterministic cutover plan report.")
    parser.add_argument(
        "--migration-findings",
        type=Path,
        default=None,
        help="Optional migration_cutover_findings.json report to link into cutover RAID and activities.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        source_report = load_json(SOURCE_REPORT_PATH)
        constraints = load_json(CONSTRAINTS_PATH)
        migration_findings_report = load_json(args.migration_findings) if args.migration_findings else None
        report = build_cutover_plan(source_report, constraints, migration_findings_report)
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
