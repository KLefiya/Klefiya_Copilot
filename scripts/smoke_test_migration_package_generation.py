from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.contracts.loader import load_migration_contract


CASES = (
    {
        "name": "Generic",
        "contract": PROJECT_ROOT / "contracts" / "generic_customer" / "datapackage.yaml",
        "data_root": PROJECT_ROOT / "data" / "examples" / "generic_customer",
        "source": PROJECT_ROOT / "data" / "examples" / "mapping" / "generic_customer" / "source_customer.csv",
        "mapping_report": PROJECT_ROOT / "data" / "synthetic" / "generic_customer_contract_mapping.json",
        "decisions": PROJECT_ROOT / "data" / "examples" / "package_generation" / "generic_customer" / "mapping_decisions.yaml",
        "output_root": PROJECT_ROOT / "data" / "generated" / "generic_customer",
        "build_report": PROJECT_ROOT / "data" / "synthetic" / "generic_customer_package_build_report.json",
        "validation_report": PROJECT_ROOT / "data" / "synthetic" / "generic_customer_generated_validation.json",
    },
    {
        "name": "SAP",
        "contract": PROJECT_ROOT / "contracts" / "sap_supplier_reference" / "datapackage.yaml",
        "data_root": PROJECT_ROOT / "data" / "examples" / "sap_supplier_reference",
        "source": PROJECT_ROOT / "data" / "examples" / "mapping" / "sap_supplier_reference" / "source_supplier.csv",
        "mapping_report": PROJECT_ROOT / "data" / "synthetic" / "sap_supplier_reference_contract_mapping.json",
        "decisions": PROJECT_ROOT / "data" / "examples" / "package_generation" / "sap_supplier_reference" / "mapping_decisions.yaml",
        "output_root": PROJECT_ROOT / "data" / "generated" / "sap_supplier_reference",
        "build_report": PROJECT_ROOT / "data" / "synthetic" / "sap_supplier_reference_package_build_report.json",
        "validation_report": PROJECT_ROOT / "data" / "synthetic" / "sap_supplier_reference_generated_validation.json",
    },
)

FORBIDDEN_CORE_STRINGS = (
    "OrganizationBPName1",
    "BusinessPartner",
    "CompanyCode",
    "PurchasingOrganization",
    "A_BusinessPartner",
    "S/4HANA",
)


def _contains_absolute_path(value) -> bool:
    if isinstance(value, dict):
        return any(_contains_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_absolute_path(item) for item in value)
    if isinstance(value, str):
        return bool(re.match(r"^[A-Za-z]:[\\/]", value))
    return False


def _core_has_adapter_branch() -> bool:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "src" / "core" / "package_generation").glob("*.py")
    )
    return bool(re.search(r"\bif\s+.*adapter\b|\belif\s+.*adapter\b|adapter\s*==|adapter\s+in", text))


def _core_has_forbidden_strings() -> bool:
    files = list((PROJECT_ROOT / "src" / "core" / "package_generation").glob("*.py"))
    files.append(PROJECT_ROOT / "src" / "tools" / "build_migration_package.py")
    return any(
        needle in path.read_text(encoding="utf-8")
        for path in files
        for needle in FORBIDDEN_CORE_STRINGS
    )


def _truth_isolated() -> bool:
    files = list((PROJECT_ROOT / "src" / "core" / "package_generation").glob("*.py"))
    files.append(PROJECT_ROOT / "src" / "tools" / "build_migration_package.py")
    forbidden = ("ground_truth.json", "ground_truth_path")
    return not any(
        pattern in path.read_text(encoding="utf-8")
        for path in files
        for pattern in forbidden
    )


def _run(case: dict) -> dict:
    args = [
        sys.executable,
        "src/tools/build_migration_package.py",
        "--contract", str(case["contract"]),
        "--contract-data-root", str(case["data_root"]),
        "--source", str(case["source"]),
        "--mapping-report", str(case["mapping_report"]),
        "--decisions", str(case["decisions"]),
        "--output-root", str(case["output_root"]),
        "--build-report", str(case["build_report"]),
        "--validation-report", str(case["validation_report"]),
    ]
    result = subprocess.run(args, cwd=PROJECT_ROOT, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr + result.stdout)
    return {
        "build": json.loads(case["build_report"].read_text(encoding="utf-8")),
        "validation": json.loads(case["validation_report"].read_text(encoding="utf-8")),
        "manifest": json.loads((case["output_root"] / "package_manifest.json").read_text(encoding="utf-8")),
    }


def main() -> int:
    loaded = [load_migration_contract(case["contract"], case["data_root"]) for case in CASES]
    first = [_run(case) for case in CASES]
    second = [_run(case) for case in CASES]
    deterministic = all(
        first_item["build"]["_run_info"]["content_sha256"] == second_item["build"]["_run_info"]["content_sha256"]
        and first_item["validation"]["_run_info"]["content_sha256"] == second_item["validation"]["_run_info"]["content_sha256"]
        and first_item["manifest"]["_run_info"]["content_sha256"] == second_item["manifest"]["_run_info"]["content_sha256"]
        for first_item, second_item in zip(first, second)
    )
    checks = {
        "contracts": len(loaded) == 2,
        "generic_resources": first[0]["build"]["summary"]["resources_generated"] == 2,
        "sap_resources": first[1]["build"]["summary"]["resources_generated"] == 2,
        "generic_valid": first[0]["validation"]["summary"]["valid"] is True,
        "sap_valid": first[1]["validation"]["summary"]["valid"] is True,
        "generic_findings": first[0]["validation"]["summary"]["finding_count"] == 0,
        "sap_findings": first[1]["validation"]["summary"]["finding_count"] == 0,
        "lineage": first[0]["build"]["summary"]["lineage_entries"] == 66 and first[1]["build"]["summary"]["lineage_entries"] == 66,
        "truth": _truth_isolated(),
        "deterministic": deterministic,
        "absolute_paths": not any(_contains_absolute_path(item) for case in first for item in case.values()),
        "adapter": not _core_has_adapter_branch(),
        "hardcoding": not _core_has_forbidden_strings(),
    }
    print("Package builder: contract-driven")
    print("Contracts: 2")
    print(f"Generic resources: {first[0]['build']['summary']['resources_generated']}")
    print(f"Generic validation: {'valid' if checks['generic_valid'] else 'invalid'}")
    print(f"Generic findings: {first[0]['validation']['summary']['finding_count']}")
    print(f"SAP resources: {first[1]['build']['summary']['resources_generated']}")
    print(f"SAP validation: {'valid' if checks['sap_valid'] else 'invalid'}")
    print(f"SAP findings: {first[1]['validation']['summary']['finding_count']}")
    print(f"Ground truth isolation: {'valid' if checks['truth'] else 'invalid'}")
    print(f"Deterministic replay: {'valid' if checks['deterministic'] else 'invalid'}")
    print(f"Adapter branching: {'none' if checks['adapter'] else 'found'}")
    valid = all(checks.values())
    print(f"Validation: {'valid' if valid else 'invalid'}")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
