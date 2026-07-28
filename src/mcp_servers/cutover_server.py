"""Local stdio MCP server for deterministic module three cutover reports.

The server intentionally exposes only a fixed whitelist of module three report
files and deterministic rebuild functions. It does not accept file paths, run
shell commands, read credentials, call an LLM, or contact SAP/network services.

Usage:
    python -m src.mcp_servers.cutover_server
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = PROJECT_ROOT / "src" / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import build_cutover_plan as cutover_plan  # noqa: E402
import build_cutover_status as cutover_status  # noqa: E402

SERVER_NAME = "carveops-cutover"
TOOL_NAMES = (
    "get_cutover_plan_summary",
    "get_cutover_status_summary",
    "get_cutover_daily_brief",
    "list_cutover_activities",
    "list_raid_items",
    "rebuild_cutover_reports",
)

SYNTHETIC_DIR = PROJECT_ROOT / "data" / "synthetic"
PLAN_REPORT_PATH = SYNTHETIC_DIR / "cutover_plan_report.json"
STATUS_REPORT_PATH = SYNTHETIC_DIR / "cutover_status_report.json"
DAILY_REPORT_PATH = SYNTHETIC_DIR / "cutover_daily_report.json"
CONSTRAINTS_PATH = SYNTHETIC_DIR / "cutover_constraints.json"
STATUS_UPDATES_PATH = SYNTHETIC_DIR / "cutover_status_updates.json"
MODULE_TWO_REPORT_PATH = SYNTHETIC_DIR / "gap_analysis_report.json"

REPORT_PATHS = {
    "plan": PLAN_REPORT_PATH,
    "status": STATUS_REPORT_PATH,
    "daily": DAILY_REPORT_PATH,
    "constraints": CONSTRAINTS_PATH,
    "updates": STATUS_UPDATES_PATH,
    "module_two": MODULE_TWO_REPORT_PATH,
}

VALID_ACTIVITY_STATUSES = set(cutover_status.ACTIVITY_STATUSES)
VALID_RAID_STATUSES = set(cutover_status.RAID_STATUSES)
VALID_RAID_TYPES = {"Risk", "Assumption", "Issue", "Dependency"}
VALID_RAID_SEVERITIES = {"Low", "Medium", "High", "Critical"}

mcp = FastMCP(SERVER_NAME, log_level="ERROR")


class McpToolError(RuntimeError):
    """Internal structured error for MCP tool envelopes."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def success(tool: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "tool": tool, "data": data}


def failure(tool: str, error: McpToolError) -> dict[str, Any]:
    return {
        "ok": False,
        "tool": tool,
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details,
        },
    }


def display_path(report_path: Path) -> str:
    try:
        return str(report_path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(report_path)


def run_tool(tool: str, handler: Any) -> dict[str, Any]:
    try:
        return success(tool, handler())
    except McpToolError as error:
        print(f"{tool} failed: {error.code}: {error.message}", file=sys.stderr)
        return failure(tool, error)
    except (cutover_plan.CutoverBuildError, cutover_status.CutoverStatusError) as error:
        print(f"{tool} rebuild failed: {error}", file=sys.stderr)
        return failure(tool, McpToolError("REBUILD_FAILED", "Deterministic rebuild failed.", {"reason": str(error)}))
    except Exception as error:  # noqa: BLE001 - do not leak traceback over MCP.
        print(f"{tool} unexpected failure: {type(error).__name__}: {error}", file=sys.stderr)
        return failure(tool, McpToolError("INVALID_REPORT", "The report could not be processed."))


def load_report(kind: str) -> dict[str, Any]:
    report_path = REPORT_PATHS[kind]
    if not report_path.is_file():
        raise McpToolError("REPORT_NOT_FOUND", f"Required report is missing: {display_path(report_path)}")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise McpToolError(
            "INVALID_REPORT",
            f"Report is not valid JSON: {display_path(report_path)}",
            {"line": error.lineno, "column": error.colno},
        ) from error
    if not isinstance(report, dict):
        raise McpToolError("INVALID_REPORT", f"Report must be a JSON object: {display_path(report_path)}")
    return report


def source_sha(report: dict[str, Any]) -> str:
    content_sha = report.get("_run_info", {}).get("content_sha256")
    if not isinstance(content_sha, str) or not content_sha:
        raise McpToolError("INVALID_REPORT", "Report is missing _run_info.content_sha256.")
    return content_sha


def require_validation(valid: bool, details: dict[str, Any] | None = None) -> None:
    if not valid:
        raise McpToolError("VALIDATION_FAILED", "Report validation failed.", details or {})


def report_validation_valid(report: dict[str, Any]) -> bool:
    validation = report.get("validation", {})
    if isinstance(validation.get("valid"), bool):
        return validation["valid"]
    if isinstance(validation.get("status"), str):
        return validation["status"] == "valid"
    return False


def offset_sort_key(offset: str) -> tuple[int, str]:
    value = cutover_plan.offset_value(offset)
    if value is None:
        value = 9999
    return (value, offset)


def compact_counter(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def summarize_plan() -> dict[str, Any]:
    report = load_report("plan")
    activities = report.get("activities")
    raid_register = report.get("raid_register")
    if not isinstance(activities, list) or not isinstance(raid_register, list):
        raise McpToolError("INVALID_REPORT", "Cutover plan report is missing activities or raid_register arrays.")
    validation_valid = report_validation_valid(report)
    return {
        "source_content_sha256": source_sha(report),
        "work_package_count": len(report.get("work_packages", [])),
        "activity_count": len(activities),
        "shared_activity_count": sum(1 for item in activities if not item.get("work_package_id")),
        "raid_count": len(raid_register),
        "freeze_window_count": len(report.get("freeze_windows", [])),
        "approval_gate_count": len(report.get("approval_gates", [])),
        "validation_valid": validation_valid,
        "activities_by_status": compact_counter([item.get("status", "") for item in activities]),
        "raid_by_type": compact_counter([item.get("type", "") for item in raid_register]),
    }


def summarize_status() -> dict[str, Any]:
    report = load_report("status")
    critical_ids = [item["activity_id"] for item in report.get("critical_blockers", [])]
    return {
        "source_content_sha256": source_sha(report),
        "as_of_offset": report.get("as_of_offset"),
        "events_applied_count": report.get("events_applied_count"),
        "activity_status_counts": report.get("activity_status_counts"),
        "work_package_status_counts": report.get("work_package_status_counts"),
        "raid_status_counts": report.get("raid_status_counts"),
        "approval_gate_status_counts": report.get("approval_gate_status_counts"),
        "critical_blocked_activity_ids": critical_ids,
        "validation_valid": report_validation_valid(report),
    }


def summarize_daily() -> dict[str, Any]:
    report = load_report("daily")
    headline = report.get("headline")
    if not isinstance(headline, dict):
        raise McpToolError("INVALID_REPORT", "Cutover daily report is missing headline.")
    progress = report.get("progress_summary", {})
    return {
        "source_content_sha256": source_sha(report),
        "as_of_offset": headline.get("as_of_offset"),
        "overall_rag": headline.get("overall_rag"),
        "headline": headline,
        "rag_reasons": report.get("rag_reasons", []),
        "critical_blockers": report.get("critical_blockers", []),
        "due_now": report.get("due_now", []),
        "due_next": report.get("due_next", []),
        "raid_summary": progress.get("raid_status_counts", {}),
        "gate_summary": progress.get("approval_gate_status_counts", {}),
        "management_actions": report.get("management_actions", []),
        "validation_valid": report_validation_valid(report),
    }


def allowed_workstreams(activities: list[dict[str, Any]]) -> set[str]:
    return {item["workstream"] for item in activities if item.get("workstream")}


def allowed_owner_roles() -> set[str]:
    constraints = load_report("constraints")
    roles = constraints.get("owner_roles")
    if not isinstance(roles, list):
        raise McpToolError("INVALID_REPORT", "Cutover constraints are missing owner_roles.")
    return {str(role) for role in roles}


def activity_view(activity: dict[str, Any]) -> dict[str, Any]:
    return {
        "activity_id": activity.get("activity_id"),
        "title": activity.get("title"),
        "workstream": activity.get("workstream"),
        "owner_role": activity.get("owner_role"),
        "start_offset": activity.get("start_offset"),
        "end_offset": activity.get("end_offset"),
        "current_status": activity.get("current_status"),
        "progress_percent": activity.get("progress_percent"),
        "blocker": activity.get("blocker"),
        "is_critical_to_day1": activity.get("is_critical_to_day1"),
        "depends_on": activity.get("depends_on", []),
        "source_requirement_id": activity.get("source_requirement_id"),
    }


def filter_activities(
    *,
    status: str | None = None,
    owner_role: str | None = None,
    workstream: str | None = None,
    critical_only: bool = False,
) -> dict[str, Any]:
    report = load_report("status")
    activities = report.get("activities")
    if not isinstance(activities, list):
        raise McpToolError("INVALID_REPORT", "Cutover status report is missing activities.")
    if status is not None and status not in VALID_ACTIVITY_STATUSES:
        raise McpToolError("INVALID_FILTER", "Invalid activity status filter.", {"allowed": sorted(VALID_ACTIVITY_STATUSES)})
    if owner_role is not None and owner_role not in allowed_owner_roles():
        raise McpToolError("INVALID_FILTER", "Invalid owner_role filter.", {"allowed": sorted(allowed_owner_roles())})
    streams = allowed_workstreams(activities)
    if workstream is not None and workstream not in streams:
        raise McpToolError("INVALID_FILTER", "Invalid workstream filter.", {"allowed": sorted(streams)})

    rows = []
    for activity in activities:
        if status is not None and activity.get("current_status") != status:
            continue
        if owner_role is not None and activity.get("owner_role") != owner_role:
            continue
        if workstream is not None and activity.get("workstream") != workstream:
            continue
        if critical_only and not activity.get("is_critical_to_day1"):
            continue
        rows.append(activity_view(activity))
    rows.sort(key=lambda item: (offset_sort_key(str(item["end_offset"])), item["activity_id"]))
    return {
        "source_content_sha256": source_sha(report),
        "count": len(rows),
        "activities": rows,
    }


def raid_view(item: dict[str, Any]) -> dict[str, Any]:
    linked_requirement = item.get("source_requirement_id")
    linked_activity_ids = [
        activity_id
        for activity_id in item.get("linked_activity_ids", [])
        if isinstance(activity_id, str)
    ]
    return {
        "raid_id": item.get("raid_id"),
        "type": item.get("type"),
        "title": item.get("title") or item.get("description"),
        "owner_role": item.get("owner_role"),
        "severity": item.get("severity"),
        "current_status": item.get("current_status"),
        "mitigation": item.get("mitigation") or item.get("mitigation_plan"),
        "trigger": item.get("trigger"),
        "linked_requirement_ids": [linked_requirement] if linked_requirement else [],
        "linked_activity_ids": linked_activity_ids,
    }


def filter_raid_items(
    *,
    raid_type: str | None = None,
    status: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    report = load_report("status")
    items = report.get("raid_register")
    if not isinstance(items, list):
        raise McpToolError("INVALID_REPORT", "Cutover status report is missing raid_register.")
    if raid_type is not None and raid_type not in VALID_RAID_TYPES:
        raise McpToolError("INVALID_FILTER", "Invalid RAID type filter.", {"allowed": sorted(VALID_RAID_TYPES)})
    if status is not None and status not in VALID_RAID_STATUSES:
        raise McpToolError("INVALID_FILTER", "Invalid RAID status filter.", {"allowed": sorted(VALID_RAID_STATUSES)})
    if severity is not None and severity not in VALID_RAID_SEVERITIES:
        raise McpToolError("INVALID_FILTER", "Invalid RAID severity filter.", {"allowed": sorted(VALID_RAID_SEVERITIES)})

    rows = []
    for item in items:
        if raid_type is not None and item.get("type") != raid_type:
            continue
        if status is not None and item.get("current_status") != status:
            continue
        if severity is not None and item.get("severity") != severity:
            continue
        rows.append(raid_view(item))
    rows.sort(key=lambda item: (item["type"] or "", item["severity"] or "", item["raid_id"] or ""))
    return {
        "source_content_sha256": source_sha(report),
        "count": len(rows),
        "raid_items": rows,
    }


def preserve_timestamp_if_same_content(new_report: dict[str, Any], output_path: Path) -> dict[str, Any]:
    if not output_path.is_file():
        return new_report
    try:
        old_report = json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return new_report
    old_run = old_report.get("_run_info", {})
    new_run = new_report.get("_run_info", {})
    if old_run.get("content_sha256") == new_run.get("content_sha256") and old_run.get("generated_at"):
        new_report["_run_info"]["generated_at"] = old_run["generated_at"]
    return new_report


def write_stable_report(report: dict[str, Any], output_path: Path) -> dict[str, Any]:
    report = preserve_timestamp_if_same_content(report, output_path)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def rebuild_reports_impl(*, rebuild_plan: bool = False) -> dict[str, Any]:
    try:
        plan_report: dict[str, Any]
        if rebuild_plan:
            source_report = cutover_plan.load_json(REPORT_PATHS["module_two"])
            constraints = cutover_plan.load_json(REPORT_PATHS["constraints"])
            raw_plan = cutover_plan.build_cutover_plan(source_report, constraints)
            if not raw_plan["validation"]["valid"]:
                raise McpToolError("VALIDATION_FAILED", "Cutover plan validation failed.", raw_plan["validation"])
            plan_report = cutover_plan.write_report(raw_plan, REPORT_PATHS["plan"])
        else:
            plan_report = load_report("plan")

        constraints = cutover_status.load_json(REPORT_PATHS["constraints"])
        updates = cutover_status.load_json(REPORT_PATHS["updates"])
        status_report = cutover_status.build_status_report(plan_report, constraints, updates)
        if status_report["validation"]["status"] != "valid":
            raise McpToolError("VALIDATION_FAILED", "Cutover status validation failed.", status_report["validation"])
        daily_report = cutover_status.build_daily_report(status_report)
        if daily_report["validation"]["status"] != "valid":
            raise McpToolError("VALIDATION_FAILED", "Cutover daily validation failed.", daily_report["validation"])
    except McpToolError:
        raise
    except (cutover_plan.CutoverBuildError, cutover_status.CutoverStatusError) as error:
        raise McpToolError(
            "VALIDATION_FAILED",
            "Deterministic cutover rebuild validation failed.",
            {"reason": str(error)},
        ) from error

    status_report = write_stable_report(status_report, REPORT_PATHS["status"])
    daily_report = write_stable_report(daily_report, REPORT_PATHS["daily"])

    return {
        "rebuild_plan": rebuild_plan,
        "validation": "valid",
        "plan_content_sha256": source_sha(plan_report),
        "status_content_sha256": source_sha(status_report),
        "daily_content_sha256": source_sha(daily_report),
        "activity_status_counts": status_report["activity_status_counts"],
        "raid_status_counts": status_report["raid_status_counts"],
        "overall_rag": daily_report["headline"]["overall_rag"],
    }


@mcp.tool()
def get_cutover_plan_summary() -> dict[str, Any]:
    """Return a compact summary of the deterministic cutover plan report."""
    return run_tool("get_cutover_plan_summary", summarize_plan)


@mcp.tool()
def get_cutover_status_summary() -> dict[str, Any]:
    """Return a compact summary of the deterministic cutover status snapshot."""
    return run_tool("get_cutover_status_summary", summarize_status)


@mcp.tool()
def get_cutover_daily_brief() -> dict[str, Any]:
    """Return the deterministic cutover daily brief without LLM summarization."""
    return run_tool("get_cutover_daily_brief", summarize_daily)


@mcp.tool()
def list_cutover_activities(
    status: str | None = None,
    owner_role: str | None = None,
    workstream: str | None = None,
    critical_only: bool = False,
) -> dict[str, Any]:
    """List cutover activities from the deterministic status report."""
    return run_tool(
        "list_cutover_activities",
        lambda: filter_activities(
            status=status,
            owner_role=owner_role,
            workstream=workstream,
            critical_only=critical_only,
        ),
    )


@mcp.tool()
def list_raid_items(
    raid_type: str | None = None,
    status: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    """List RAID items from the deterministic status report."""
    return run_tool(
        "list_raid_items",
        lambda: filter_raid_items(raid_type=raid_type, status=status, severity=severity),
    )


@mcp.tool()
def rebuild_cutover_reports(rebuild_plan: bool = False) -> dict[str, Any]:
    """Rebuild deterministic cutover reports by importing existing Python builders."""
    return run_tool("rebuild_cutover_reports", lambda: rebuild_reports_impl(rebuild_plan=rebuild_plan))


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
