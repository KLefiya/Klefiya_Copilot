from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.contracts.loader import (
    ContractLoadError,
    load_migration_contract,
)
from src.core.contracts.validator import (
    _category,
    load_and_validate,
    validate_migration_contract,
    write_validation_report,
)


GENERIC_CONTRACT = PROJECT_ROOT / "contracts" / "generic_customer" / "datapackage.yaml"
GENERIC_DATA = PROJECT_ROOT / "data" / "examples" / "generic_customer"
SUPPLIER_CONTRACT = (
    PROJECT_ROOT / "contracts" / "sap_supplier_reference" / "datapackage.yaml"
)
SUPPLIER_DATA = PROJECT_ROOT / "data" / "examples" / "sap_supplier_reference"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _categories(report: dict) -> set[str]:
    return set(report["summary"]["by_category"])


def _write_temp_contract(
    temp_root: Path,
    descriptor: dict,
    resource_name: str = "sample.csv",
) -> tuple[Path, Path]:
    data_root = temp_root / "data"
    data_root.mkdir()
    (data_root / resource_name).write_text("id,name\n1,A\n", encoding="utf-8")
    descriptor_path = temp_root / "datapackage.yaml"
    descriptor_path.write_text(yaml.safe_dump(descriptor), encoding="utf-8")
    return descriptor_path, data_root


def _minimal_descriptor(path: str = "sample.csv") -> dict:
    return {
        "profile": "tabular-data-package",
        "name": "minimal-contract",
        "version": "1.0.0",
        "carveops": {
            "contract_id": "minimal-v1",
            "adapter": "generic_csv",
            "domain": "sample",
            "synthetic": True,
            "authoritative": False,
        },
        "resources": [
            {
                "profile": "tabular-data-resource",
                "name": "sample",
                "path": path,
                "schema": {
                    "fields": [
                        {
                            "name": "id",
                            "type": "string",
                            "constraints": {"required": True},
                        },
                        {"name": "name", "type": "string"},
                    ]
                },
            }
        ],
    }


class MigrationContractTests(unittest.TestCase):
    def test_01_frictionless_is_pinned(self):
        import importlib.metadata

        self.assertEqual(importlib.metadata.version("frictionless"), "5.19.0")

    def test_02_generic_contract_loads_metadata(self):
        contract = load_migration_contract(GENERIC_CONTRACT, GENERIC_DATA)
        self.assertEqual(contract.contract_id, "generic-customer-v1")
        self.assertEqual(contract.adapter, "generic_csv")
        self.assertEqual(contract.domain, "customer_master")
        self.assertTrue(contract.synthetic)
        self.assertFalse(contract.authoritative)

    def test_03_supplier_contract_loads_metadata(self):
        contract = load_migration_contract(SUPPLIER_CONTRACT, SUPPLIER_DATA)
        self.assertEqual(contract.contract_id, "sap-supplier-reference-v1")
        self.assertEqual(contract.adapter, "sap_supplier_reference")
        self.assertEqual(contract.domain, "supplier_master")
        self.assertTrue(contract.synthetic)
        self.assertFalse(contract.authoritative)

    def test_04_generic_resource_count(self):
        contract = load_migration_contract(GENERIC_CONTRACT, GENERIC_DATA)
        self.assertEqual(contract.resource_names, ("customer", "customer_bank"))

    def test_05_supplier_resource_count(self):
        contract = load_migration_contract(SUPPLIER_CONTRACT, SUPPLIER_DATA)
        self.assertEqual(
            contract.resource_names, ("supplier_general", "supplier_company")
        )

    def test_06_descriptor_sha_is_stable_hex(self):
        contract = load_migration_contract(GENERIC_CONTRACT, GENERIC_DATA)
        self.assertRegex(contract.descriptor_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(contract.descriptor_sha256, _sha(GENERIC_CONTRACT))

    def test_07_missing_carveops_metadata_fails(self):
        descriptor = _minimal_descriptor()
        descriptor.pop("carveops")
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            path, data = _write_temp_contract(Path(temp_dir), descriptor)
            with self.assertRaises(ContractLoadError) as ctx:
                load_migration_contract(path, data)
        self.assertEqual(ctx.exception.code, "missing_required_metadata")

    def test_08_missing_resource_file_fails(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            path, data = _write_temp_contract(Path(temp_dir), _minimal_descriptor("nope.csv"))
            with self.assertRaises(ContractLoadError) as ctx:
                load_migration_contract(path, data)
        self.assertEqual(ctx.exception.code, "resource_missing")

    def test_09_resource_path_escape_fails(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            path, data = _write_temp_contract(Path(temp_dir), _minimal_descriptor("../sample.csv"))
            with self.assertRaises(ContractLoadError) as ctx:
                load_migration_contract(path, data)
        self.assertEqual(ctx.exception.code, "path_escape_not_allowed")

    def test_10_remote_resource_fails(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            path, data = _write_temp_contract(
                Path(temp_dir), _minimal_descriptor("https://example.invalid/sample.csv")
            )
            with self.assertRaises(ContractLoadError) as ctx:
                load_migration_contract(path, data)
        self.assertEqual(ctx.exception.code, "remote_source_not_allowed")

    def test_11_absolute_descriptor_path_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path, data = _write_temp_contract(root, _minimal_descriptor())
            with self.assertRaises(ContractLoadError) as ctx:
                load_migration_contract(path, data)
        self.assertEqual(ctx.exception.code, "path_outside_project")

    def test_12_secret_like_descriptor_value_fails(self):
        descriptor = _minimal_descriptor()
        descriptor["carveops"]["source_note"] = "s" + "k-" + "this-is-not-a-real-key"
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            path, data = _write_temp_contract(Path(temp_dir), descriptor)
            with self.assertRaises(ContractLoadError) as ctx:
                load_migration_contract(path, data)
        self.assertEqual(ctx.exception.code, "secret_like_value")

    def test_13_generic_report_is_invalid(self):
        report = load_and_validate(GENERIC_CONTRACT, GENERIC_DATA)
        self.assertFalse(report["validation"]["valid"])
        self.assertFalse(report["summary"]["valid"])

    def test_14_supplier_report_is_invalid(self):
        report = load_and_validate(SUPPLIER_CONTRACT, SUPPLIER_DATA)
        self.assertFalse(report["validation"]["valid"])
        self.assertFalse(report["summary"]["valid"])

    def test_15_generic_expected_categories_present(self):
        report = load_and_validate(GENERIC_CONTRACT, GENERIC_DATA)
        self.assertTrue(
            {"required", "unique", "primary_key", "foreign_key", "enum"}.issubset(
                _categories(report)
            )
        )

    def test_16_supplier_expected_categories_present(self):
        report = load_and_validate(SUPPLIER_CONTRACT, SUPPLIER_DATA)
        self.assertTrue(
            {"required", "unique", "primary_key", "foreign_key", "enum"}.issubset(
                _categories(report)
            )
        )

    def test_17_pattern_and_max_length_are_normalized(self):
        report = load_and_validate(GENERIC_CONTRACT, GENERIC_DATA)
        self.assertIn("pattern", _categories(report))
        self.assertIn("max_length", _categories(report))

    def test_18_supplier_pattern_and_max_length_are_normalized(self):
        report = load_and_validate(SUPPLIER_CONTRACT, SUPPLIER_DATA)
        self.assertIn("pattern", _categories(report))
        self.assertIn("max_length", _categories(report))

    def test_19_findings_have_required_shape(self):
        report = load_and_validate(GENERIC_CONTRACT, GENERIC_DATA)
        keys = {
            "resource",
            "row_number",
            "field",
            "category",
            "raw_code",
            "severity",
            "message",
            "note",
            "cells",
        }
        self.assertTrue(report["findings"])
        self.assertTrue(keys.issubset(report["findings"][0]))

    def test_20_findings_are_deterministically_sorted(self):
        report = load_and_validate(GENERIC_CONTRACT, GENERIC_DATA)
        ordered = [
            (
                item["resource"],
                item["row_number"] if item["row_number"] is not None else -1,
                item["field"] or "",
                item["category"],
                item["raw_code"],
                item["message"],
            )
            for item in report["findings"]
        ]
        self.assertEqual(ordered, sorted(ordered))

    def test_21_report_paths_are_project_relative(self):
        report = load_and_validate(GENERIC_CONTRACT, GENERIC_DATA)
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotRegex(text, r"[A-Za-z]:[\\/]")
        self.assertEqual(
            report["_meta"]["contract_path"],
            "contracts/generic_customer/datapackage.yaml",
        )

    def test_22_report_meta_has_contract_sha(self):
        report = load_and_validate(SUPPLIER_CONTRACT, SUPPLIER_DATA)
        self.assertEqual(report["_meta"]["contract_sha256"], _sha(SUPPLIER_CONTRACT))

    def test_23_resource_row_counts_are_actual(self):
        report = load_and_validate(GENERIC_CONTRACT, GENERIC_DATA)
        self.assertEqual(report["_meta"]["row_count"], 10)
        counts = {item["name"]: item["row_count"] for item in report["resources"]}
        self.assertEqual(counts, {"customer": 6, "customer_bank": 4})

    def test_24_supplier_resource_row_counts_are_actual(self):
        report = load_and_validate(SUPPLIER_CONTRACT, SUPPLIER_DATA)
        self.assertEqual(report["_meta"]["row_count"], 10)
        counts = {item["name"]: item["row_count"] for item in report["resources"]}
        self.assertEqual(counts, {"supplier_general": 6, "supplier_company": 4})

    def test_25_by_resource_matches_resource_findings(self):
        report = load_and_validate(GENERIC_CONTRACT, GENERIC_DATA)
        counted = {}
        for item in report["findings"]:
            counted[item["resource"]] = counted.get(item["resource"], 0) + 1
        self.assertEqual(report["summary"]["by_resource"], dict(sorted(counted.items())))

    def test_26_by_severity_matches_findings(self):
        report = load_and_validate(SUPPLIER_CONTRACT, SUPPLIER_DATA)
        counted = {}
        for item in report["findings"]:
            counted[item["severity"]] = counted.get(item["severity"], 0) + 1
        self.assertEqual(report["summary"]["by_severity"], dict(sorted(counted.items())))

    def test_27_content_sha_is_stable_across_replay(self):
        first = load_and_validate(GENERIC_CONTRACT, GENERIC_DATA)
        second = load_and_validate(GENERIC_CONTRACT, GENERIC_DATA)
        self.assertEqual(
            first["_run_info"]["content_sha256"],
            second["_run_info"]["content_sha256"],
        )

    def test_28_generated_at_is_preserved_for_same_content(self):
        report = load_and_validate(GENERIC_CONTRACT, GENERIC_DATA)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            output = Path(temp_dir) / "report.json"
            write_validation_report(report, output)
            first = json.loads(output.read_text(encoding="utf-8"))
            changed = load_and_validate(GENERIC_CONTRACT, GENERIC_DATA)
            write_validation_report(changed, output)
            second = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(first["_run_info"]["generated_at"], second["_run_info"]["generated_at"])

    def test_29_content_sha_changes_when_contract_changes(self):
        descriptor = yaml.safe_load(GENERIC_CONTRACT.read_text(encoding="utf-8"))
        descriptor["version"] = "1.0.1"
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            data_root.mkdir()
            for csv_file in GENERIC_DATA.glob("*.csv"):
                (data_root / csv_file.name).write_bytes(csv_file.read_bytes())
            path = root / "datapackage.yaml"
            path.write_text(yaml.safe_dump(descriptor), encoding="utf-8")
            changed = load_and_validate(path, data_root)
        original = load_and_validate(GENERIC_CONTRACT, GENERIC_DATA)
        self.assertNotEqual(
            original["_run_info"]["content_sha256"],
            changed["_run_info"]["content_sha256"],
        )

    def test_30_unknown_raw_code_is_preserved_as_other(self):
        self.assertEqual(_category({"type": "future-code"}), "other")

    def test_31_cli_generates_report_and_returns_zero_by_default(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            output = Path(temp_dir) / "generic.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "src/tools/validate_migration_package.py",
                    "--contract",
                    str(GENERIC_CONTRACT),
                    "--data-root",
                    str(GENERIC_DATA),
                    "--output",
                    str(output),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())
            self.assertIn("Contract        : generic-customer-v1", result.stdout)

    def test_32_cli_strict_returns_one_for_invalid_data(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            output = Path(temp_dir) / "generic.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "src/tools/validate_migration_package.py",
                    "--contract",
                    str(GENERIC_CONTRACT),
                    "--data-root",
                    str(GENERIC_DATA),
                    "--output",
                    str(output),
                    "--strict",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertTrue(output.exists())

    def test_33_smoke_script_passes(self):
        result = subprocess.run(
            [sys.executable, "scripts/smoke_test_migration_contracts.py"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Validation: valid", result.stdout)

    def test_34_inputs_are_not_modified_by_cli(self):
        files = sorted(GENERIC_DATA.glob("*.csv"))
        before = {path.name: _sha(path) for path in files}
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            output = Path(temp_dir) / "generic.json"
            subprocess.run(
                [
                    sys.executable,
                    "src/tools/validate_migration_package.py",
                    "--contract",
                    str(GENERIC_CONTRACT),
                    "--data-root",
                    str(GENERIC_DATA),
                    "--output",
                    str(output),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
        after = {path.name: _sha(path) for path in files}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
