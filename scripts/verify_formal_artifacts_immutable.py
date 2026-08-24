from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_NAME = "carveops_formal_artifacts_snapshot.json"

FORMAL_ARTIFACTS = (
    "data/generated/erpnext_item_price_multitarget/item.csv",
    "data/generated/erpnext_item_price_multitarget/item_price.csv",
    "data/generated/erpnext_item_price_multitarget/lineage.json",
    "data/generated/erpnext_item_price_multitarget/package_manifest.json",
    "data/generated/generic_customer/customer.csv",
    "data/generated/generic_customer/customer_bank.csv",
    "data/generated/generic_customer/lineage.json",
    "data/generated/generic_customer/package_manifest.json",
    "data/generated/sap_supplier_reference/lineage.json",
    "data/generated/sap_supplier_reference/package_manifest.json",
    "data/generated/sap_supplier_reference/supplier_company.csv",
    "data/generated/sap_supplier_reference/supplier_general.csv",
    "data/synthetic/cutover_agent_runs/079d010f577e19c6.json",
    "data/synthetic/cutover_agent_runs/7ec74a0244f541b4.json",
    "data/synthetic/cutover_agent_runs/8c6ab3a485c7863c.json",
    "data/synthetic/cutover_agent_runs/afd93286ba294a1a.json",
    "data/synthetic/cutover_agent_runs/b0b2fe615513f73f.json",
    "data/synthetic/cutover_agent_runs/c3d12316c8ed0ed5.json",
    "data/synthetic/cutover_agent_trace.json",
    "data/synthetic/cutover_daily_report.json",
    "data/synthetic/cutover_plan_report.json",
    "data/synthetic/cutover_status_report.json",
    "data/synthetic/cutover_status_updates.json",
    "data/synthetic/erpnext_item_price_blind_evaluation.json",
    "data/synthetic/erpnext_item_price_blind_mapping.json",
    "data/synthetic/erpnext_item_price_multitarget_build_report.json",
    "data/synthetic/erpnext_item_price_multitarget_generated_validation.json",
    "data/synthetic/erpnext_item_price_multitarget_remediation.json",
    "data/synthetic/generic_customer_contract_mapping.json",
    "data/synthetic/generic_customer_contract_mapping_evaluation.json",
    "data/synthetic/generic_customer_contract_validation.json",
    "data/synthetic/generic_customer_generated_validation.json",
    "data/synthetic/generic_customer_package_build_report.json",
    "data/synthetic/migration_cutover_findings.json",
    "data/synthetic/sap_supplier_reference_contract_mapping.json",
    "data/synthetic/sap_supplier_reference_contract_mapping_evaluation.json",
    "data/synthetic/sap_supplier_reference_contract_validation.json",
    "data/synthetic/sap_supplier_reference_generated_validation.json",
    "data/synthetic/sap_supplier_reference_package_build_report.json",
    "data/synthetic/schema_matching_precision_tiered_v4_5scenario_evaluation.json",
    "data/synthetic/schema_matching_precision_tiered_v5_5scenario_evaluation.json",
    "data/synthetic/vendor_duplicate_report.json",
    "data/synthetic/vendor_field_mapping.json",
    "data/synthetic/vendor_profile_report.json",
    "data/synthetic/vendor_validation_report.json",
)

EXPECTED_AGENT_RUNS = tuple(
    artifact
    for artifact in FORMAL_ARTIFACTS
    if artifact.startswith("data/synthetic/cutover_agent_runs/")
)


def default_snapshot_path() -> Path:
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    return Path(base) / SNAPSHOT_NAME


def raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_inventory() -> list[dict[str, object]]:
    return [
        {
            "path": artifact,
            "exists": (PROJECT_ROOT / artifact).exists(),
            "raw_sha256": raw_sha256(PROJECT_ROOT / artifact) if (PROJECT_ROOT / artifact).exists() else None,
        }
        for artifact in FORMAL_ARTIFACTS
    ]


def agent_run_errors() -> list[str]:
    actual = sorted(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "data/synthetic/cutover_agent_runs").glob("*.json")
    )
    expected = sorted(EXPECTED_AGENT_RUNS)
    errors: list[str] = []
    if len(actual) != len(expected):
        errors.append(f"expected {len(expected)} formal agent runs, found {len(actual)}")
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing:
        errors.append("missing formal agent runs: " + ", ".join(missing))
    if extra:
        errors.append("extra formal agent runs: " + ", ".join(extra))
    return errors


def write_snapshot(path: Path) -> int:
    inventory = current_inventory()
    missing = [item["path"] for item in inventory if not item["exists"]]
    errors = agent_run_errors()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"snapshot": str(path), "formal_count": len(inventory), "missing_count": len(missing), "errors": errors}, indent=2))
    return 1 if missing or errors else 0


def verify_snapshot(path: Path) -> int:
    if not path.exists():
        print(json.dumps({"error": "snapshot_missing", "snapshot": str(path)}, indent=2))
        return 1

    before = json.loads(path.read_text(encoding="utf-8"))
    before_by_path = {item["path"]: item for item in before}
    now = current_inventory()
    changed = []
    missing = []
    for item in now:
        path_key = item["path"]
        previous = before_by_path.get(path_key)
        if not item["exists"]:
            missing.append(path_key)
        elif previous is None or previous.get("raw_sha256") != item.get("raw_sha256"):
            changed.append(
                {
                    "path": path_key,
                    "before": previous.get("raw_sha256") if previous else None,
                    "after": item.get("raw_sha256"),
                }
            )
    removed = sorted(set(before_by_path) - {item["path"] for item in now})
    errors = agent_run_errors()
    result = {
        "formal_count": len(now),
        "changed_count": len(changed),
        "missing_count": len(missing),
        "removed_count": len(removed),
        "changed": changed,
        "missing": missing,
        "removed": removed,
        "errors": errors,
    }
    print(json.dumps(result, indent=2))
    return 1 if changed or missing or removed or errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot and verify the formal CarveOps artifacts.")
    parser.add_argument("command", choices=("snapshot", "verify"))
    parser.add_argument("--snapshot", type=Path, default=default_snapshot_path())
    args = parser.parse_args(argv)

    if args.command == "snapshot":
        return write_snapshot(args.snapshot)
    return verify_snapshot(args.snapshot)


if __name__ == "__main__":
    sys.exit(main())
