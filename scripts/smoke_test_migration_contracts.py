from __future__ import annotations

import json
import re
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.contracts import load_migration_contract, validate_migration_contract
from src.core.contracts.validator import write_validation_report


FORBIDDEN_CORE_STRINGS = (
    "OrganizationBPName1",
    "BusinessPartner",
    "CompanyCode",
    "PurchasingOrganization",
    "SAP",
    "S/4HANA",
)


def _validate(contract_path: Path, data_root: Path) -> dict:
    contract = load_migration_contract(contract_path, data_root)
    return validate_migration_contract(contract)


def _stable_replay(report: dict, output: Path) -> bool:
    write_validation_report(report, output)
    first = json.loads(output.read_text(encoding="utf-8"))["_run_info"][
        "content_sha256"
    ]
    write_validation_report(report, output)
    second = json.loads(output.read_text(encoding="utf-8"))["_run_info"][
        "content_sha256"
    ]
    return first == second


def _contains_absolute_path(value) -> bool:
    if isinstance(value, dict):
        return any(_contains_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_absolute_path(item) for item in value)
    if isinstance(value, str):
        return bool(re.match(r"^[A-Za-z]:[\\/]", value))
    return False


def _core_contains_forbidden_strings() -> bool:
    files = [
        PROJECT_ROOT / "src" / "core" / "contracts" / "loader.py",
        PROJECT_ROOT / "src" / "core" / "contracts" / "validator.py",
        PROJECT_ROOT / "src" / "tools" / "validate_migration_package.py",
    ]
    return any(
        needle in path.read_text(encoding="utf-8")
        for path in files
        for needle in FORBIDDEN_CORE_STRINGS
    )


def main() -> int:
    generic = _validate(
        PROJECT_ROOT / "contracts" / "generic_customer" / "datapackage.yaml",
        PROJECT_ROOT / "data" / "examples" / "generic_customer",
    )
    supplier = _validate(
        PROJECT_ROOT / "contracts" / "sap_supplier_reference" / "datapackage.yaml",
        PROJECT_ROOT / "data" / "examples" / "sap_supplier_reference",
    )
    expected_categories = {"required", "unique", "primary_key", "foreign_key", "enum"}
    generic_categories = set(generic["summary"]["by_category"])
    supplier_categories = set(supplier["summary"]["by_category"])
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
        replay_valid = _stable_replay(generic, Path(temp_dir) / "generic.json")
    checks = {
        "generic_invalid": generic["validation"]["valid"] is False,
        "supplier_invalid": supplier["validation"]["valid"] is False,
        "generic_categories": expected_categories.issubset(generic_categories),
        "supplier_categories": expected_categories.issubset(supplier_categories),
        "replay": replay_valid,
        "absolute_paths": not _contains_absolute_path(generic)
        and not _contains_absolute_path(supplier),
        "hardcoding": not _core_contains_forbidden_strings(),
    }
    print(f"Validation engine: frictionless {version('frictionless')}")
    print("Contracts: 2")
    print(f"Generic customer resources: {generic['_meta']['resource_count']}")
    print(f"Generic customer findings: {generic['summary']['finding_count']}")
    print(
        f"SAP supplier reference resources: {supplier['_meta']['resource_count']}"
    )
    print(f"SAP supplier reference findings: {supplier['summary']['finding_count']}")
    print(
        "Generic expected categories: "
        f"{'present' if checks['generic_categories'] else 'missing'}"
    )
    print(
        "SAP expected categories: "
        f"{'present' if checks['supplier_categories'] else 'missing'}"
    )
    print(f"Deterministic replay: {'valid' if checks['replay'] else 'invalid'}")
    print(f"Absolute paths: {'none' if checks['absolute_paths'] else 'found'}")
    print(f"Core SAP hardcoding: {'none' if checks['hardcoding'] else 'found'}")
    valid = all(checks.values())
    print(f"Validation: {'valid' if valid else 'invalid'}")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
