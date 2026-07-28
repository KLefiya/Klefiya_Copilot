"""Apply deterministic cutover status events and build execution reports.

This module intentionally avoids LLM calls, network requests, SAP APIs, and
evaluation or ground-truth inputs. It consumes the deterministic module three
cutover plan plus an append-only event log, then writes a status snapshot and a
daily management report.

Usage:
    python src/tools/build_cutover_status.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict, deque
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from build_cutover_plan import offset_value  # noqa: E402
from data_profile import attach_run_info  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_DIR = PROJECT_ROOT / "data" / "synthetic"
PLAN_PATH = SYNTHETIC_DIR / "cutover_plan_report.json"
CONSTRAINTS_PATH = SYNTHETIC_DIR / "cutover_constraints.json"
UPDATES_PATH = SYNTHETIC_DIR / "cutover_status_updates.json"
STATUS_OUTPUT_PATH = SYNTHETIC_DIR / "cutover_status_report.json"
DAILY_OUTPUT_PATH = SYNTHETIC_DIR / "cutover_daily_report.json"

PLAN_REL = "data/synthetic/cutover_plan_report.json"
CONSTRAINTS_REL = "data/synthetic/cutover_constraints.json"
UPDATES_REL = "data/synthetic/cutover_status_updates.json"
STATUS_REL = "data/synthetic/cutover_status_report.json"

EXPECTED_SOURCE_PLAN_SHA = "c4a88a3cb0923d2ed28356f72c037ace313ee73bee961ae8212265e4de2a0a8d"
DEFAULT_AS_OF_OFFSET = "T-7"
DAY1_TARGET_ACTIVITY_ID = "CUT-DAY1-VALIDATION"

ENTITY_TYPES = {"Activity", "RAID", "ApprovalGate"}
ACTIVITY_STATUSES = ("Not Started", "In Progress", "Blocked", "Completed", "Cancelled")
RAID_STATUSES = ("Open", "Mitigating", "Accepted", "Resolved", "Closed")
GATE_STATUSES = ("Pending", "Ready", "Approved", "Rejected", "Blocked")
WORK_PACKAGE_STATUSES = ("Not Started", "In Progress", "Blocked", "Completed", "Cancelled")

ACTIVITY_TRANSITIONS = {
    "Not Started": {"In Progress", "Blocked", "Cancelled", "Completed"},
    "In Progress": {"Blocked", "Completed", "Cancelled"},
    "Blocked": {"In Progress", "Completed", "Cancelled"},
    "Completed": set(),
    "Cancelled": set(),
}
RAID_TRANSITIONS = {
    "Open": {"Mitigating", "Accepted", "Resolved", "Closed"},
    "Mitigating": {"Accepted", "Resolved", "Closed"},
    "Accepted": {"Resolved", "Closed"},
    "Resolved": {"Closed"},
    "Closed": set(),
}
GATE_TRANSITIONS = {
    "Pending": {"Ready", "Blocked"},
    "Ready": {"Approved", "Rejected", "Blocked"},
    "Blocked": {"Ready", "Rejected"},
    "Approved": set(),
    "Rejected": set(),
}

PHASE_ORDER = {"DESIGN": 0, "BUILD": 1, "TEST": 2, "DEPLOY": 3}
EVENT_REQUIRED_FIELDS = (
    "event_id",
    "sequence",
    "effective_offset",
    "entity_type",
    "entity_id",
    "new_status",
    "progress_percent",
    "updated_by_role",
    "note",
    "blocker",
    "evidence",
)


class CutoverStatusError(RuntimeError):
    """Raised when status events or source reports are invalid."""


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def load_json(path: Path) -> Any:
    if not path.is_file():
        raise CutoverStatusError(f"Required input is missing: {display_path(path)}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CutoverStatusError(f"Invalid JSON in {display_path(path)}: {error}") from error


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def status_counter(values: list[str], statuses: tuple[str, ...]) -> dict[str, int]:
    counts = Counter(values)
    return {status: counts.get(status, 0) for status in statuses}


def require_offset(offset: str, label: str) -> int:
    value = offset_value(offset)
    if value is None:
        raise CutoverStatusError(f"{label} has an invalid offset: {offset}")
    return value


def require_inputs(plan: dict[str, Any], constraints: dict[str, Any], updates: dict[str, Any]) -> None:
    for key in ("_run_info", "activities", "work_packages", "raid_register", "approval_gates"):
        if key not in plan:
            raise CutoverStatusError(f"Cutover plan is missing `{key}`.")
    source_sha = plan.get("_run_info", {}).get("content_sha256")
    if source_sha != EXPECTED_SOURCE_PLAN_SHA:
        raise CutoverStatusError(
            f"Cutover plan SHA mismatch: expected {EXPECTED_SOURCE_PLAN_SHA}, got {source_sha}"
        )
    if not isinstance(constraints.get("owner_roles"), list) or not constraints["owner_roles"]:
        raise CutoverStatusError("Cutover constraints are missing owner_roles.")
    if not isinstance(updates.get("events"), list):
        raise CutoverStatusError("Cutover status updates must contain an events array.")


def sorted_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    event_ids: set[str] = set()
    sequences: set[int] = set()
    for event in events:
        if not isinstance(event, dict):
            raise CutoverStatusError("Each status event must be a JSON object.")
        for field in EVENT_REQUIRED_FIELDS:
            if field not in event:
                raise CutoverStatusError(f"Status event is missing `{field}`.")
        event_id = event["event_id"]
        sequence = event["sequence"]
        if not isinstance(event_id, str) or not event_id:
            raise CutoverStatusError("Event id must be a non-empty string.")
        if not isinstance(sequence, int):
            raise CutoverStatusError(f"Event {event_id} has a non-integer sequence.")
        if event_id in event_ids:
            raise CutoverStatusError(f"Duplicate event_id: {event_id}")
        if sequence in sequences:
            raise CutoverStatusError(f"Duplicate sequence: {sequence}")
        event_ids.add(event_id)
        sequences.add(sequence)
    return sorted(events, key=lambda event: (event["sequence"], event["event_id"]))


def activity_phase(activity_id: str) -> str | None:
    suffix = activity_id.rsplit("-", 1)[-1]
    return suffix if suffix in PHASE_ORDER else None


def initial_activity_state(activity: dict[str, Any]) -> dict[str, Any]:
    status = activity.get("status", "Not Started")
    if status not in ACTIVITY_STATUSES:
        raise CutoverStatusError(f"Activity {activity.get('activity_id')} has invalid status `{status}`.")
    return {
        **deepcopy(activity),
        "current_status": status,
        "progress_percent": 100 if status == "Completed" else 0,
        "last_event_id": None,
        "last_update_offset": None,
        "last_note": "",
        "blocker": "",
        "is_critical_to_day1": False,
    }


def initial_raid_state(item: dict[str, Any]) -> dict[str, Any]:
    status = item.get("status", "Open")
    if status not in RAID_STATUSES:
        raise CutoverStatusError(f"RAID {item.get('raid_id')} has invalid status `{status}`.")
    return {
        **deepcopy(item),
        "current_status": status,
        "last_event_id": None,
        "last_update_offset": None,
        "last_note": "",
    }


def initial_gate_state(gate: dict[str, Any]) -> dict[str, Any]:
    return {
        **deepcopy(gate),
        "current_status": "Pending",
        "last_event_id": None,
        "last_update_offset": None,
        "last_note": "",
        "blocker": "",
        "readiness": False,
        "missing_readiness_criteria": [],
    }


def allowed_transition(current: str, new_status: str, transitions: dict[str, set[str]]) -> bool:
    return current == new_status or new_status in transitions.get(current, set())


def validate_progress(event: dict[str, Any]) -> None:
    event_id = event["event_id"]
    entity_type = event["entity_type"]
    new_status = event["new_status"]
    progress = event["progress_percent"]
    if entity_type != "Activity":
        if progress is not None:
            raise CutoverStatusError(f"Event {event_id} must not set progress for {entity_type}.")
        return
    if not isinstance(progress, int) or progress < 0 or progress > 100:
        raise CutoverStatusError(f"Event {event_id} has invalid activity progress.")
    if new_status == "Not Started" and progress != 0:
        raise CutoverStatusError(f"Event {event_id} must set Not Started progress to 0.")
    if new_status == "Completed" and progress != 100:
        raise CutoverStatusError(f"Event {event_id} must set Completed progress to 100.")
    if new_status == "In Progress" and not 1 <= progress <= 99:
        raise CutoverStatusError(f"Event {event_id} must set In Progress progress from 1 to 99.")
    if new_status == "Blocked" and not 0 <= progress <= 99:
        raise CutoverStatusError(f"Event {event_id} must set Blocked progress from 0 to 99.")


def high_open_risks_or_issues(raids: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        item["raid_id"]
        for item in raids.values()
        if item.get("type") in {"Risk", "Issue"}
        and item.get("severity") == "High"
        and item.get("current_status") in {"Open", "Mitigating"}
    )


def gate_readiness(
    gate_id: str,
    activities: dict[str, dict[str, Any]],
    raids: dict[str, dict[str, Any]],
    gates: dict[str, dict[str, Any]],
) -> tuple[bool, list[str]]:
    missing: list[str] = []

    def activity_done(activity_id: str) -> bool:
        return activities[activity_id]["current_status"] == "Completed"

    def activities_by_suffix(suffix: str) -> list[str]:
        return sorted(activity_id for activity_id in activities if activity_id.endswith(f"-{suffix}"))

    def require_done(activity_ids: list[str], label: str) -> None:
        incomplete = [activity_id for activity_id in activity_ids if not activity_done(activity_id)]
        if incomplete:
            missing.append(f"{label} incomplete: {', '.join(incomplete)}")

    risk_ids = high_open_risks_or_issues(raids)

    if gate_id == "GATE-DESIGN-SIGNOFF":
        require_done(activities_by_suffix("DESIGN"), "DESIGN activities")
        require_done(["CUT-SCOPE-BASELINE"], "Scope baseline")
    elif gate_id == "GATE-CUTOVER-READINESS":
        require_done(activities_by_suffix("BUILD"), "BUILD activities")
        require_done(activities_by_suffix("TEST"), "TEST activities")
        require_done(["CUT-INTEGRATION-READINESS", "CUT-CUTOVER-READINESS"], "Readiness milestones")
        if risk_ids:
            missing.append(f"Open or mitigating high Risk/Issue items: {', '.join(risk_ids)}")
    elif gate_id == "GATE-GO-NOGO":
        if gates["GATE-CUTOVER-READINESS"]["current_status"] != "Approved":
            missing.append("GATE-CUTOVER-READINESS is not Approved.")
        require_done(["CUT-CODE-FREEZE", "CUT-DATA-FREEZE"], "Freeze milestones")
        if activities["CUT-GO-NOGO"]["current_status"] == "Blocked":
            missing.append("CUT-GO-NOGO is Blocked.")
        bad_deploy = [
            activity_id
            for activity_id in activities_by_suffix("DEPLOY")
            if activities[activity_id]["current_status"] in {"Blocked", "Cancelled"}
        ]
        if bad_deploy:
            missing.append(f"DEPLOY activities blocked or cancelled: {', '.join(bad_deploy)}")
        if risk_ids:
            missing.append(f"Open or mitigating high Risk/Issue items: {', '.join(risk_ids)}")
    elif gate_id == "GATE-HYPERCARE-HANDOVER":
        require_done(["CUT-DAY1-VALIDATION", "CUT-RECONCILIATION"], "Hypercare handover prerequisites")
        if risk_ids:
            missing.append(f"Open or mitigating high Risk/Issue items: {', '.join(risk_ids)}")
    else:
        missing.append(f"Unknown gate: {gate_id}")

    return not missing, missing


def validate_common_event(
    event: dict[str, Any],
    *,
    owner_roles: set[str],
    as_of_offset: str,
) -> None:
    event_id = event["event_id"]
    entity_type = event["entity_type"]
    new_status = event["new_status"]
    if entity_type not in ENTITY_TYPES:
        raise CutoverStatusError(f"Event {event_id} has invalid entity_type `{entity_type}`.")
    if require_offset(event["effective_offset"], f"Event {event_id}") > require_offset(as_of_offset, "as_of_offset"):
        raise CutoverStatusError(f"Event {event_id} is later than as_of_offset {as_of_offset}.")
    if event["updated_by_role"] not in owner_roles:
        raise CutoverStatusError(f"Event {event_id} uses unknown owner role `{event['updated_by_role']}`.")
    if not isinstance(event["evidence"], list) or any(not isinstance(item, str) for item in event["evidence"]):
        raise CutoverStatusError(f"Event {event_id} evidence must be a list of strings.")
    blocker = event["blocker"]
    if new_status == "Blocked":
        if not isinstance(blocker, str) or not blocker.strip():
            raise CutoverStatusError(f"Event {event_id} must include a blocker for Blocked status.")
    elif blocker:
        raise CutoverStatusError(f"Event {event_id} must not include a blocker for non-Blocked status.")
    validate_progress(event)


def apply_activity_event(
    event: dict[str, Any],
    activities: dict[str, dict[str, Any]],
) -> None:
    entity_id = event["entity_id"]
    if entity_id not in activities:
        raise CutoverStatusError(f"Event {event['event_id']} references unknown activity `{entity_id}`.")
    state = activities[entity_id]
    new_status = event["new_status"]
    if new_status not in ACTIVITY_STATUSES:
        raise CutoverStatusError(f"Event {event['event_id']} has invalid activity status `{new_status}`.")
    if not allowed_transition(state["current_status"], new_status, ACTIVITY_TRANSITIONS):
        raise CutoverStatusError(
            f"Event {event['event_id']} has illegal activity transition "
            f"{state['current_status']} -> {new_status}."
        )
    if new_status == "Completed":
        incomplete = [
            dep
            for dep in state.get("depends_on", [])
            if activities[dep]["current_status"] not in {"Completed", "Cancelled"}
        ]
        if incomplete:
            raise CutoverStatusError(
                f"Event {event['event_id']} cannot complete {entity_id}; dependencies incomplete: "
                f"{', '.join(incomplete)}"
            )
    state["current_status"] = new_status
    state["progress_percent"] = event["progress_percent"]
    state["last_event_id"] = event["event_id"]
    state["last_update_offset"] = event["effective_offset"]
    state["last_note"] = event["note"]
    state["blocker"] = event["blocker"]


def apply_raid_event(event: dict[str, Any], raids: dict[str, dict[str, Any]]) -> None:
    entity_id = event["entity_id"]
    if entity_id not in raids:
        raise CutoverStatusError(f"Event {event['event_id']} references unknown RAID item `{entity_id}`.")
    state = raids[entity_id]
    new_status = event["new_status"]
    if new_status not in RAID_STATUSES:
        raise CutoverStatusError(f"Event {event['event_id']} has invalid RAID status `{new_status}`.")
    if not allowed_transition(state["current_status"], new_status, RAID_TRANSITIONS):
        raise CutoverStatusError(
            f"Event {event['event_id']} has illegal RAID transition {state['current_status']} -> {new_status}."
        )
    state["current_status"] = new_status
    state["last_event_id"] = event["event_id"]
    state["last_update_offset"] = event["effective_offset"]
    state["last_note"] = event["note"]


def apply_gate_event(
    event: dict[str, Any],
    activities: dict[str, dict[str, Any]],
    raids: dict[str, dict[str, Any]],
    gates: dict[str, dict[str, Any]],
) -> None:
    entity_id = event["entity_id"]
    if entity_id not in gates:
        raise CutoverStatusError(f"Event {event['event_id']} references unknown approval gate `{entity_id}`.")
    state = gates[entity_id]
    new_status = event["new_status"]
    if new_status not in GATE_STATUSES:
        raise CutoverStatusError(f"Event {event['event_id']} has invalid gate status `{new_status}`.")
    current = state["current_status"]
    if not allowed_transition(current, new_status, GATE_TRANSITIONS):
        raise CutoverStatusError(
            f"Event {event['event_id']} has illegal gate transition {current} -> {new_status}."
        )
    ready, missing = gate_readiness(entity_id, activities, raids, gates)
    if new_status == "Approved" and not ready:
        raise CutoverStatusError(
            f"Event {event['event_id']} cannot approve {entity_id}; missing readiness: {'; '.join(missing)}"
        )
    state["current_status"] = new_status
    state["last_event_id"] = event["event_id"]
    state["last_update_offset"] = event["effective_offset"]
    state["last_note"] = event["note"]
    state["blocker"] = event["blocker"]


def mark_critical_path(activities: dict[str, dict[str, Any]]) -> None:
    dependencies = {
        activity_id: list(activity.get("depends_on", []))
        for activity_id, activity in activities.items()
    }
    visited: set[str] = set()
    queue: deque[str] = deque([DAY1_TARGET_ACTIVITY_ID])
    while queue:
        activity_id = queue.popleft()
        if activity_id in visited or activity_id not in activities:
            continue
        visited.add(activity_id)
        queue.extend(dependencies.get(activity_id, []))
    for activity_id, activity in activities.items():
        activity["is_critical_to_day1"] = activity_id in visited


def derive_work_packages(
    plan_work_packages: list[dict[str, Any]],
    activities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_wp: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for activity in activities.values():
        work_package_id = activity.get("work_package_id")
        if work_package_id:
            by_wp[work_package_id].append(activity)

    packages: list[dict[str, Any]] = []
    for package in sorted(plan_work_packages, key=lambda item: item["work_package_id"]):
        work_package_id = package["work_package_id"]
        package_activities = sorted(
            by_wp[work_package_id],
            key=lambda item: (PHASE_ORDER.get(activity_phase(item["activity_id"]) or "", 99), item["activity_id"]),
        )
        statuses = [item["current_status"] for item in package_activities]
        if all(status == "Cancelled" for status in statuses):
            current_status = "Cancelled"
        elif all(status == "Completed" for status in statuses):
            current_status = "Completed"
        elif any(status == "Blocked" for status in statuses):
            current_status = "Blocked"
        elif any(status != "Not Started" for status in statuses):
            current_status = "In Progress"
        else:
            current_status = "Not Started"
        progress = round(
            sum(item["progress_percent"] for item in package_activities) / len(package_activities),
            2,
        )
        next_activity = next(
            (
                item["activity_id"]
                for item in package_activities
                if item["current_status"] not in {"Completed", "Cancelled"}
            ),
            None,
        )
        packages.append({
            **deepcopy(package),
            "current_status": current_status,
            "progress_percent": progress,
            "activity_status_counts": status_counter(statuses, ACTIVITY_STATUSES),
            "next_activity_id": next_activity,
        })
    return packages


def finalize_gate_states(
    activities: dict[str, dict[str, Any]],
    raids: dict[str, dict[str, Any]],
    gates: dict[str, dict[str, Any]],
) -> None:
    for gate_id in sorted(gates):
        ready, missing = gate_readiness(gate_id, activities, raids, gates)
        gates[gate_id]["readiness"] = ready
        gates[gate_id]["missing_readiness_criteria"] = missing


def normalize_activity(activity: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "activity_id",
        "work_package_id",
        "title",
        "workstream",
        "owner_role",
        "start_offset",
        "end_offset",
        "depends_on",
        "approval_gate",
        "current_status",
        "progress_percent",
        "is_critical_to_day1",
        "last_event_id",
        "last_update_offset",
        "last_note",
        "blocker",
    ]
    return {key: activity.get(key) for key in keys}


def normalize_raid(item: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "raid_id",
        "type",
        "severity",
        "description",
        "owner_role",
        "source_requirement_id",
        "current_status",
        "last_event_id",
        "last_update_offset",
        "last_note",
    ]
    return {key: item.get(key) for key in keys}


def normalize_gate(gate: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "gate_id",
        "name",
        "due_offset",
        "approver_roles",
        "entry_criteria",
        "current_status",
        "readiness",
        "missing_readiness_criteria",
        "last_event_id",
        "last_update_offset",
        "last_note",
        "blocker",
    ]
    return {key: gate.get(key) for key in keys}


def validate_event_log(
    events: list[dict[str, Any]],
    plan: dict[str, Any],
    constraints: dict[str, Any],
    as_of_offset: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    owner_roles = set(constraints["owner_roles"])
    activities = {item["activity_id"]: initial_activity_state(item) for item in plan["activities"]}
    raids = {item["raid_id"]: initial_raid_state(item) for item in plan["raid_register"]}
    gates = {item["gate_id"]: initial_gate_state(item) for item in plan["approval_gates"]}
    ordered_events = sorted_events(events)

    for event in ordered_events:
        validate_common_event(event, owner_roles=owner_roles, as_of_offset=as_of_offset)
        entity_type = event["entity_type"]
        if entity_type == "Activity":
            apply_activity_event(event, activities)
        elif entity_type == "RAID":
            apply_raid_event(event, raids)
        elif entity_type == "ApprovalGate":
            apply_gate_event(event, activities, raids, gates)
        else:
            raise CutoverStatusError(f"Unsupported entity type: {entity_type}")

    mark_critical_path(activities)
    finalize_gate_states(activities, raids, gates)
    return activities, raids, gates, ordered_events


def build_status_report(
    plan: dict[str, Any],
    constraints: dict[str, Any],
    updates: dict[str, Any],
    *,
    as_of_offset: str | None = None,
) -> dict[str, Any]:
    require_inputs(plan, constraints, updates)
    as_of = as_of_offset or updates.get("_meta", {}).get("as_of_offset") or DEFAULT_AS_OF_OFFSET
    require_offset(as_of, "as_of_offset")
    events = updates["events"]
    activities, raids, gates, ordered_events = validate_event_log(events, plan, constraints, as_of)
    work_packages = derive_work_packages(plan["work_packages"], activities)
    normalized_activities = [normalize_activity(activities[key]) for key in sorted(activities)]
    normalized_raids = [normalize_raid(raids[key]) for key in sorted(raids)]
    normalized_gates = [normalize_gate(gates[key]) for key in sorted(gates)]
    critical_blockers = [
        {
            "activity_id": activity["activity_id"],
            "title": activity["title"],
            "blocker": activity["blocker"],
            "owner_role": activity["owner_role"],
            "end_offset": activity["end_offset"],
        }
        for activity in normalized_activities
        if activity["current_status"] == "Blocked" and activity["is_critical_to_day1"]
    ]

    raid_by_type: dict[str, dict[str, int]] = {}
    for raid_type in sorted({item["type"] for item in normalized_raids}):
        raid_by_type[raid_type] = status_counter(
            [item["current_status"] for item in normalized_raids if item["type"] == raid_type],
            RAID_STATUSES,
        )

    body = {
        "_meta": {
            "tool": "src/tools/build_cutover_status.py",
            "source_plan": PLAN_REL,
            "source_plan_content_sha256": EXPECTED_SOURCE_PLAN_SHA,
            "source_constraints": CONSTRAINTS_REL,
            "source_status_updates": UPDATES_REL,
            "as_of_offset": as_of,
            "event_ordering": "Events are applied by ascending sequence and event_id.",
            "validation_scope": [
                "entity existence",
                "owner role",
                "offset",
                "status transition",
                "progress",
                "dependency completion",
                "approval gate readiness",
            ],
        },
        "as_of_offset": as_of,
        "source_plan_content_sha256": plan["_run_info"]["content_sha256"],
        "events_applied_count": len(ordered_events),
        "events_applied": deepcopy(ordered_events),
        "activity_status_counts": status_counter(
            [item["current_status"] for item in normalized_activities],
            ACTIVITY_STATUSES,
        ),
        "work_package_status_counts": status_counter(
            [item["current_status"] for item in work_packages],
            WORK_PACKAGE_STATUSES,
        ),
        "raid_status_counts": {
            "by_status": status_counter([item["current_status"] for item in normalized_raids], RAID_STATUSES),
            "by_type": raid_by_type,
        },
        "approval_gate_status_counts": status_counter(
            [item["current_status"] for item in normalized_gates],
            GATE_STATUSES,
        ),
        "activities": normalized_activities,
        "work_packages": work_packages,
        "raid_register": normalized_raids,
        "approval_gates": normalized_gates,
        "critical_blockers": critical_blockers,
        "validation": {
            "status": "valid",
            "source_plan_sha_matches": True,
            "event_ids_unique": True,
            "sequences_unique": True,
            "all_entities_resolved": True,
            "transitions_valid": True,
            "dependencies_valid": True,
            "gate_readiness_valid": True,
            "future_events": 0,
            "error_count": 0,
        },
    }
    return attach_run_info(body)


def offset_sort_key(offset: str) -> tuple[int, str]:
    return (require_offset(offset, offset), offset)


def due_bucket(
    activities: list[dict[str, Any]],
    *,
    as_of_offset: str,
    mode: str,
) -> list[dict[str, Any]]:
    as_of_value = require_offset(as_of_offset, "as_of_offset")
    rows: list[dict[str, Any]] = []
    for activity in activities:
        status = activity["current_status"]
        if status in {"Completed", "Cancelled"}:
            continue
        end_value = require_offset(activity["end_offset"], f"{activity['activity_id']}.end_offset")
        include = (
            (mode == "due_now" and end_value == as_of_value)
            or (mode == "overdue" and end_value < as_of_value)
            or (mode == "due_next" and as_of_value < end_value <= 0)
        )
        if include:
            rows.append({
                "activity_id": activity["activity_id"],
                "title": activity["title"],
                "owner_role": activity["owner_role"],
                "end_offset": activity["end_offset"],
                "current_status": activity["current_status"],
                "blocker": activity.get("blocker", ""),
                "is_critical_to_day1": activity["is_critical_to_day1"],
            })
    return sorted(rows, key=lambda item: (offset_sort_key(item["end_offset"]), item["activity_id"]))


def next_gate(gates: list[dict[str, Any]], as_of_offset: str) -> dict[str, Any] | None:
    as_of_value = require_offset(as_of_offset, "as_of_offset")
    open_gates = [
        gate
        for gate in gates
        if gate["current_status"] not in {"Approved", "Rejected"}
        and require_offset(gate["due_offset"], f"{gate['gate_id']}.due_offset") >= as_of_value
    ]
    if not open_gates:
        return None
    gate = sorted(open_gates, key=lambda item: (offset_sort_key(item["due_offset"]), item["gate_id"]))[0]
    return {
        "gate_id": gate["gate_id"],
        "name": gate["name"],
        "due_offset": gate["due_offset"],
        "current_status": gate["current_status"],
        "readiness": gate["readiness"],
        "missing_readiness_criteria": gate["missing_readiness_criteria"],
    }


def determine_rag(status_report: dict[str, Any], due_now: list[dict[str, Any]], overdue: list[dict[str, Any]]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    critical_blockers = status_report["critical_blockers"]
    if critical_blockers:
        reasons.append("One or more Day-1 critical activities are blocked.")
    blocked_due_gate = [
        gate["gate_id"]
        for gate in status_report["approval_gates"]
        if gate["current_status"] == "Blocked" and require_offset(gate["due_offset"], gate["gate_id"]) <= require_offset(status_report["as_of_offset"], "as_of_offset")
    ]
    if blocked_due_gate:
        reasons.append(f"Due approval gate is blocked: {', '.join(sorted(blocked_due_gate))}.")
    high_items = [
        item["raid_id"]
        for item in status_report["raid_register"]
        if item["type"] in {"Risk", "Issue"}
        and item.get("severity") == "High"
        and item["current_status"] in {"Open", "Mitigating"}
    ]
    if high_items:
        reasons.append(f"High Risk/Issue items remain open or mitigating: {', '.join(sorted(high_items))}.")
    if overdue:
        reasons.append("One or more activities are overdue.")
    if reasons:
        return "Red", reasons
    if due_now or any(
        item["type"] in {"Risk", "Issue"} and item["current_status"] in {"Open", "Mitigating"}
        for item in status_report["raid_register"]
    ):
        return "Amber", ["No Red rule triggered, but due work or open Risk/Issue items require management attention."]
    return "Green", ["No blocked critical activities, overdue activities, or open Risk/Issue concerns."]


def management_actions(status_report: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for blocker in status_report["critical_blockers"]:
        actions.append({
            "priority": 1,
            "source_type": "Activity",
            "source_id": blocker["activity_id"],
            "owner_role": blocker["owner_role"],
            "action": f"Resolve {blocker['activity_id']} blocker and capture closure evidence before the Cutover Readiness gate is revisited.",
        })
    for gate in status_report["approval_gates"]:
        if gate["current_status"] == "Blocked":
            actions.append({
                "priority": 2,
                "source_type": "ApprovalGate",
                "source_id": gate["gate_id"],
                "owner_role": gate["approver_roles"][0],
                "action": f"Reassess {gate['gate_id']} after missing readiness criteria are cleared.",
            })
    for item in status_report["raid_register"]:
        if item["type"] in {"Risk", "Issue"} and item["current_status"] in {"Open", "Mitigating"}:
            actions.append({
                "priority": 3,
                "source_type": "RAID",
                "source_id": item["raid_id"],
                "owner_role": item["owner_role"],
                "action": f"Confirm mitigation owner, due date, and escalation path for {item['raid_id']}.",
            })
    return sorted(actions, key=lambda item: (item["priority"], item["source_type"], item["source_id"]))


def build_daily_report(status_report: dict[str, Any]) -> dict[str, Any]:
    as_of = status_report["as_of_offset"]
    activities = status_report["activities"]
    gates = status_report["approval_gates"]
    due_now = due_bucket(activities, as_of_offset=as_of, mode="due_now")
    overdue = due_bucket(activities, as_of_offset=as_of, mode="overdue")
    due_next = due_bucket(activities, as_of_offset=as_of, mode="due_next")
    overall_rag, rag_reasons = determine_rag(status_report, due_now, overdue)
    total_activities = len(activities)
    completed_count = status_report["activity_status_counts"]["Completed"]
    blocked_count = status_report["activity_status_counts"]["Blocked"]
    not_started_count = status_report["activity_status_counts"]["Not Started"]
    open_high = high_open_risks_or_issues({
        item["raid_id"]: item
        for item in status_report["raid_register"]
    })
    actions = management_actions(status_report)

    body = {
        "_meta": {
            "tool": "src/tools/build_cutover_status.py",
            "source_status_report": STATUS_REL,
            "source_status_report_content_sha256": status_report["_run_info"]["content_sha256"],
            "as_of_offset": as_of,
            "rag_rules": [
                "Red if a Day-1 critical activity is blocked.",
                "Red if a due approval gate is blocked.",
                "Red if a high Risk or Issue is Open or Mitigating.",
                "Red if any incomplete activity is overdue.",
                "Amber if due work or non-high Risk/Issue items require attention.",
                "Green otherwise.",
            ],
        },
        "headline": {
            "overall_rag": overall_rag,
            "as_of_offset": as_of,
            "completed_activity_count": completed_count,
            "blocked_activity_count": blocked_count,
            "not_started_activity_count": not_started_count,
            "work_packages_blocked": status_report["work_package_status_counts"]["Blocked"],
            "open_high_risks_or_issues": len(open_high),
            "next_gate": next_gate(gates, as_of),
        },
        "rag_reasons": rag_reasons,
        "progress_summary": {
            "activities_total": total_activities,
            "activity_status_counts": status_report["activity_status_counts"],
            "activity_completion_percent": round(completed_count / total_activities * 100, 2),
            "work_packages_total": len(status_report["work_packages"]),
            "work_package_status_counts": status_report["work_package_status_counts"],
            "raid_total": len(status_report["raid_register"]),
            "raid_status_counts": status_report["raid_status_counts"],
            "approval_gate_total": len(gates),
            "approval_gate_status_counts": status_report["approval_gate_status_counts"],
        },
        "due_now": due_now,
        "overdue": overdue,
        "due_next": due_next,
        "critical_blockers": status_report["critical_blockers"],
        "management_actions": actions,
        "validation": {
            "status": "valid",
            "source_status_report_sha": status_report["_run_info"]["content_sha256"],
            "source_status_report_valid": status_report["validation"]["status"] == "valid",
        },
    }
    return attach_run_info(body)


def main() -> int:
    try:
        plan = load_json(PLAN_PATH)
        constraints = load_json(CONSTRAINTS_PATH)
        updates = load_json(UPDATES_PATH)
        status_report = build_status_report(plan, constraints, updates)
        daily_report = build_daily_report(status_report)
        write_json(STATUS_OUTPUT_PATH, status_report)
        write_json(DAILY_OUTPUT_PATH, daily_report)
    except CutoverStatusError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"As of offset: {status_report['as_of_offset']}")
    print(f"Events applied: {status_report['events_applied_count']}")
    print(f"Activities: {len(status_report['activities'])}")
    print(f"Completed: {status_report['activity_status_counts']['Completed']}")
    print(f"Blocked: {status_report['activity_status_counts']['Blocked']}")
    print(f"Not Started: {status_report['activity_status_counts']['Not Started']}")
    print(f"Work packages: {len(status_report['work_packages'])}")
    print(f"Work packages blocked: {status_report['work_package_status_counts']['Blocked']}")
    print(f"RAID items: {len(status_report['raid_register'])}")
    print(f"Approval gates: {len(status_report['approval_gates'])}")
    print(f"Overall RAG: {daily_report['headline']['overall_rag']}")
    print(f"Validation: {status_report['validation']['status']}")
    print(f"Status content SHA: {status_report['_run_info']['content_sha256']}")
    print(f"Daily content SHA: {daily_report['_run_info']['content_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
