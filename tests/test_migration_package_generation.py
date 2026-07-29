from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.contracts.loader import load_migration_contract
from src.core.package_generation.builder import (
    PackageBuildBlocked,
    build_migration_package,
)
from src.core.package_generation.decision_loader import (
    DecisionLoadError,
    load_mapping_decisions,
)


GENERIC_CONTRACT = PROJECT_ROOT / "contracts" / "generic_customer" / "datapackage.yaml"
GENERIC_DATA = PROJECT_ROOT / "data" / "examples" / "generic_customer"
GENERIC_SOURCE = PROJECT_ROOT / "data" / "examples" / "mapping" / "generic_customer" / "source_customer.csv"
GENERIC_MAPPING = PROJECT_ROOT / "data" / "synthetic" / "generic_customer_contract_mapping.json"
GENERIC_DECISIONS = PROJECT_ROOT / "data" / "examples" / "package_generation" / "generic_customer" / "mapping_decisions.yaml"
SAP_CONTRACT = PROJECT_ROOT / "contracts" / "sap_supplier_reference" / "datapackage.yaml"
SAP_DATA = PROJECT_ROOT / "data" / "examples" / "sap_supplier_reference"
SAP_SOURCE = PROJECT_ROOT / "data" / "examples" / "mapping" / "sap_supplier_reference" / "source_supplier.csv"
SAP_MAPPING = PROJECT_ROOT / "data" / "synthetic" / "sap_supplier_reference_contract_mapping.json"
SAP_DECISIONS = PROJECT_ROOT / "data" / "examples" / "package_generation" / "sap_supplier_reference" / "mapping_decisions.yaml"
GENERATED_ROOT = PROJECT_ROOT / "data" / "generated"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _contract(path=GENERIC_CONTRACT, data=GENERIC_DATA):
    return load_migration_contract(path, data)


def _load_yaml(path=GENERIC_DECISIONS) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_yaml(root: Path, document: dict, name: str = "mapping_decisions.yaml") -> Path:
    path = root / name
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _write_mapping(root: Path, report: dict, name: str = "mapping.json") -> Path:
    path = root / name
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _build_temp(decisions=GENERIC_DECISIONS, source=GENERIC_SOURCE, mapping=GENERIC_MAPPING, contract=None):
    contract = contract or _contract()
    temp = tempfile.TemporaryDirectory(dir=GENERATED_ROOT)
    output = Path(temp.name) / "package"
    validation = Path(temp.name) / "validation.json"
    report = build_migration_package(
        contract,
        source,
        mapping,
        decisions,
        output,
        validation_report_path=validation,
    )
    return temp, output, validation, report


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _cli_args(case: str, output_root: Path, build_report: Path, validation_report: Path, *, decisions=None, mapping=None):
    if case == "generic":
        return [
            sys.executable, "src/tools/build_migration_package.py",
            "--contract", str(GENERIC_CONTRACT),
            "--contract-data-root", str(GENERIC_DATA),
            "--source", str(GENERIC_SOURCE),
            "--mapping-report", str(mapping or GENERIC_MAPPING),
            "--decisions", str(decisions or GENERIC_DECISIONS),
            "--output-root", str(output_root),
            "--build-report", str(build_report),
            "--validation-report", str(validation_report),
        ]
    return [
        sys.executable, "src/tools/build_migration_package.py",
        "--contract", str(SAP_CONTRACT),
        "--contract-data-root", str(SAP_DATA),
        "--source", str(SAP_SOURCE),
        "--mapping-report", str(mapping or SAP_MAPPING),
        "--decisions", str(decisions or SAP_DECISIONS),
        "--output-root", str(output_root),
        "--build-report", str(build_report),
        "--validation-report", str(validation_report),
    ]


class MigrationPackageGenerationTests(unittest.TestCase):
    def test_01_valid_generic_decisions_load(self):
        decisions = load_mapping_decisions(GENERIC_DECISIONS, _contract(), GENERIC_MAPPING)
        self.assertEqual(len(decisions.approved()), 11)

    def test_02_valid_sap_decisions_load(self):
        decisions = load_mapping_decisions(SAP_DECISIONS, _contract(SAP_CONTRACT, SAP_DATA), SAP_MAPPING)
        self.assertEqual(len(decisions.rejected()), 1)

    def test_03_wrong_contract_id_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["contract_id"] = "wrong"
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), GENERIC_MAPPING)
        self.assertEqual(ctx.exception.code, "contract_id_mismatch")

    def test_04_stale_contract_sha_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            report = json.loads(GENERIC_MAPPING.read_text(encoding="utf-8"))
            report["_meta"]["contract_sha256"] = "0" * 64
            mapping = _write_mapping(Path(root), report)
            doc = _load_yaml()
            doc["mapping_report"] = _rel(mapping)
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), mapping)
        self.assertEqual(ctx.exception.code, "stale_contract_sha")

    def test_05_stale_source_sha_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            report = json.loads(GENERIC_MAPPING.read_text(encoding="utf-8"))
            report["_meta"]["source_sha256"] = "1" * 64
            mapping = _write_mapping(Path(root), report)
            doc = _load_yaml()
            doc["mapping_report"] = _rel(mapping)
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), mapping)
        self.assertEqual(ctx.exception.code, "stale_source_sha")

    def test_06_stale_mapping_report_path_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            mapping = Path(root) / "other.json"
            mapping.write_bytes(GENERIC_MAPPING.read_bytes())
            doc = _load_yaml()
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), mapping)
        self.assertEqual(ctx.exception.code, "mapping_report_mismatch")

    def test_07_missing_source_field_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][0]["source_field"] = "missing_source"
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), GENERIC_MAPPING)
        self.assertEqual(ctx.exception.code, "source_field_missing")

    def test_08_unknown_target_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][0]["target"] = "customer.nope"
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), GENERIC_MAPPING)
        self.assertEqual(ctx.exception.code, "unknown_target")

    def test_09_duplicate_approved_link_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"].append(dict(doc["decisions"][0]))
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), GENERIC_MAPPING)
        self.assertEqual(ctx.exception.code, "duplicate_approved_link")

    def test_10_duplicate_approved_target_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][1]["target"] = "customer.customer_id"
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), GENERIC_MAPPING)
        self.assertEqual(ctx.exception.code, "duplicate_approved_target")

    def test_11_approved_without_target_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][0]["target"] = None
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), GENERIC_MAPPING)
        self.assertEqual(ctx.exception.code, "approved_target_required")

    def test_12_rejected_with_target_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][-1]["target"] = "customer.email"
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), GENERIC_MAPPING)
        self.assertEqual(ctx.exception.code, "target_not_allowed")

    def test_13_deferred_with_target_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][-1]["decision"] = "deferred"
            doc["decisions"][-1]["target"] = "customer.email"
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), GENERIC_MAPPING)
        self.assertEqual(ctx.exception.code, "target_not_allowed")

    def test_14_target_not_in_top3_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][0]["target"] = "customer.email"
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), GENERIC_MAPPING)
        self.assertEqual(ctx.exception.code, "target_not_in_top3")

    def test_15_url_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["source"]["path"] = "https://example.invalid/source.csv"
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), GENERIC_MAPPING)
        self.assertEqual(ctx.exception.code, "remote_path_not_allowed")

    def test_16_path_escape_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["source"]["path"] = "../source.csv"
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), GENERIC_MAPPING)
        self.assertEqual(ctx.exception.code, "unsafe_path_not_allowed")

    def test_17_unsupported_transformation_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][0]["transformation"] = {"type": "eval"}
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), GENERIC_MAPPING)
        self.assertEqual(ctx.exception.code, "unsupported_transformation")

    def test_18_ground_truth_reference_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][-1]["reason"] = "see ground_truth.json"
            path = _write_yaml(Path(root), doc)
            with self.assertRaises(DecisionLoadError) as ctx:
                load_mapping_decisions(path, _contract(), GENERIC_MAPPING)
        self.assertEqual(ctx.exception.code, "ground_truth_reference_not_allowed")

    def test_19_one_engine_for_two_contracts(self):
        with tempfile.TemporaryDirectory(dir=GENERATED_ROOT) as generic_root, tempfile.TemporaryDirectory(dir=GENERATED_ROOT) as sap_root:
            generic = build_migration_package(_contract(), GENERIC_SOURCE, GENERIC_MAPPING, GENERIC_DECISIONS, Path(generic_root) / "g")
            sap = build_migration_package(_contract(SAP_CONTRACT, SAP_DATA), SAP_SOURCE, SAP_MAPPING, SAP_DECISIONS, Path(sap_root) / "s")
            self.assertEqual(generic["_meta"]["component"], sap["_meta"]["component"])

    def test_20_contract_resource_order(self):
        temp, _, _, report = _build_temp()
        self.addCleanup(temp.cleanup)
        self.assertEqual([item["resource"] for item in report["resources"]], ["customer", "customer_bank"])

    def test_21_contract_field_order(self):
        temp, output, _, _ = _build_temp()
        self.addCleanup(temp.cleanup)
        with (output / "customer.csv").open("r", encoding="utf-8") as handle:
            header = handle.readline().strip()
        self.assertEqual(header, "customer_id,customer_name,country,email,phone,tax_number,payment_terms")

    def test_22_source_row_order(self):
        temp, output, _, _ = _build_temp()
        self.addCleanup(temp.cleanup)
        rows = _read_csv(output / "customer.csv")
        self.assertEqual([row["customer_id"] for row in rows][:2], ["LC-1001", "LC-1002"])

    def test_23_copy_transformation(self):
        temp, output, _, _ = _build_temp()
        self.addCleanup(temp.cleanup)
        self.assertEqual(_read_csv(output / "customer.csv")[0]["customer_name"], "Synthetic Aurora Stores")

    def test_24_constant_transformation(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][3]["transformation"] = {"type": "constant", "value": "constant@example.test"}
            path = _write_yaml(Path(root), doc)
            temp, output, _, _ = _build_temp(decisions=path)
            self.addCleanup(temp.cleanup)
            self.assertEqual(_read_csv(output / "customer.csv")[0]["email"], "constant@example.test")

    def test_25_value_map_transformation(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][2]["transformation"] = {"type": "value_map", "values": {"DE": "DE", "US": "US", "GB": "GB"}, "on_missing": "reject_row"}
            path = _write_yaml(Path(root), doc)
            temp, output, _, _ = _build_temp(decisions=path)
            self.addCleanup(temp.cleanup)
            self.assertEqual(_read_csv(output / "customer.csv")[1]["country"], "US")

    def test_26_value_map_reject_row(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][2]["transformation"] = {"type": "value_map", "values": {"DE": "DE", "GB": "GB"}, "on_missing": "reject_row"}
            path = _write_yaml(Path(root), doc)
            temp, _, _, report = _build_temp(decisions=path)
            self.addCleanup(temp.cleanup)
            self.assertEqual(report["summary"]["rejected_rows"], 2)

    def test_27_value_map_keep_original(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][2]["transformation"] = {"type": "value_map", "values": {"DE": "DE"}, "on_missing": "keep_original"}
            path = _write_yaml(Path(root), doc)
            temp, output, _, _ = _build_temp(decisions=path)
            self.addCleanup(temp.cleanup)
            self.assertEqual(_read_csv(output / "customer.csv")[1]["country"], "US")

    def test_28_value_map_empty(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][3]["transformation"] = {"type": "value_map", "values": {}, "on_missing": "empty"}
            path = _write_yaml(Path(root), doc)
            temp, output, _, _ = _build_temp(decisions=path)
            self.addCleanup(temp.cleanup)
            self.assertEqual(_read_csv(output / "customer.csv")[0]["email"], "")

    def test_29_required_unmapped_blocks(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][2]["decision"] = "deferred"
            doc["decisions"][2]["target"] = None
            path = _write_yaml(Path(root), doc)
            with tempfile.TemporaryDirectory(dir=GENERATED_ROOT) as out:
                with self.assertRaises(PackageBuildBlocked):
                    build_migration_package(_contract(), GENERIC_SOURCE, GENERIC_MAPPING, path, Path(out) / "package")

    def test_30_primary_key_unmapped_blocks(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][0]["decision"] = "rejected"
            doc["decisions"][0]["target"] = None
            path = _write_yaml(Path(root), doc)
            with tempfile.TemporaryDirectory(dir=GENERATED_ROOT) as out:
                with self.assertRaises(PackageBuildBlocked):
                    build_migration_package(_contract(), GENERIC_SOURCE, GENERIC_MAPPING, path, Path(out) / "package")

    def test_31_optional_unmapped_outputs_empty(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][3]["decision"] = "rejected"
            doc["decisions"][3]["target"] = None
            path = _write_yaml(Path(root), doc)
            temp, output, _, report = _build_temp(decisions=path)
            self.addCleanup(temp.cleanup)
            self.assertEqual(_read_csv(output / "customer.csv")[0]["email"], "")
            self.assertIn("customer.email", report["unmapped_target_fields"])

    def test_32_empty_optional_resource_row_skipped(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            root_path = Path(root)
            source = root_path / "source.csv"
            source.write_text("id,opt\nR1,\n", encoding="utf-8")
            data = root_path / "data"
            data.mkdir()
            (data / "optional.csv").write_text("optional_note\n", encoding="utf-8")
            descriptor = {
                "profile": "tabular-data-package",
                "name": "optional-package",
                "version": "1.0.0",
                "carveops": {"contract_id": "optional-v1", "adapter": "fixture", "domain": "fixture", "synthetic": True, "authoritative": False},
                "resources": [{"profile": "tabular-data-resource", "name": "optional", "path": "optional.csv", "schema": {"fields": [{"name": "optional_note", "type": "string"}]}}],
            }
            contract_path = root_path / "datapackage.yaml"
            contract_path.write_text(yaml.safe_dump(descriptor), encoding="utf-8")
            contract = load_migration_contract(contract_path, data)
            mapping = {
                "_meta": {"contract_id": "optional-v1", "contract_sha256": contract.descriptor_sha256, "source_sha256": _sha(source), "source_row_count": 1, "source_field_count": 2},
                "mappings": [{"source_field": "opt", "top_candidates": [{"target": "optional.optional_note"}]}],
            }
            mapping_path = _write_mapping(root_path, mapping)
            doc = {"version": "1.0.0", "contract_id": "optional-v1", "mapping_report": _rel(mapping_path), "source": {"path": _rel(source), "record_id_field": "id"}, "decisions": [{"source_field": "opt", "target": "optional.optional_note", "decision": "approved", "transformation": {"type": "copy"}}]}
            decision_path = _write_yaml(root_path, doc)
            with tempfile.TemporaryDirectory(dir=GENERATED_ROOT) as out:
                report = build_migration_package(contract, source, mapping_path, decision_path, Path(out) / "optional")
                self.assertEqual(report["resources"][0]["row_count"], 0)

    def test_33_input_source_unchanged(self):
        before = _sha(GENERIC_SOURCE)
        temp, _, _, _ = _build_temp()
        self.addCleanup(temp.cleanup)
        self.assertEqual(_sha(GENERIC_SOURCE), before)

    def test_34_atomic_output_success_writes_manifest(self):
        temp, output, _, _ = _build_temp()
        self.addCleanup(temp.cleanup)
        self.assertTrue((output / "package_manifest.json").exists())

    def test_35_failure_leaves_no_partial_formal_package(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            doc["decisions"][0]["target"] = "customer.email"
            path = _write_yaml(Path(root), doc)
            with tempfile.TemporaryDirectory(dir=GENERATED_ROOT) as out:
                output = Path(out) / "bad"
                with self.assertRaises(DecisionLoadError):
                    build_migration_package(_contract(), GENERIC_SOURCE, GENERIC_MAPPING, path, output)
                self.assertFalse(output.exists())

    def test_36_lineage_entry_count(self):
        temp, _, _, report = _build_temp()
        self.addCleanup(temp.cleanup)
        self.assertEqual(report["summary"]["lineage_entries"], 66)

    def test_37_lineage_no_raw_sensitive_value(self):
        temp, output, _, _ = _build_temp()
        self.addCleanup(temp.cleanup)
        text = (output / "lineage.json").read_text(encoding="utf-8")
        self.assertNotIn("Synthetic Aurora Stores", text)

    def test_38_lineage_source_value_sha(self):
        temp, output, _, _ = _build_temp()
        self.addCleanup(temp.cleanup)
        lineage = json.loads((output / "lineage.json").read_text(encoding="utf-8"))
        entry = next(item for item in lineage["entries"] if item["source_field"] == "legacy_client_id")
        self.assertEqual(entry["source_value_sha256"], hashlib.sha256("LC-1001".encode()).hexdigest())

    def test_39_relative_paths(self):
        temp, output, _, report = _build_temp()
        self.addCleanup(temp.cleanup)
        text = json.dumps(report, ensure_ascii=False) + (output / "package_manifest.json").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"[A-Za-z]:[\\/]")

    def test_40_deterministic_ordering(self):
        temp, output, _, _ = _build_temp()
        self.addCleanup(temp.cleanup)
        lineage = json.loads((output / "lineage.json").read_text(encoding="utf-8"))
        keys = [(e["target_resource"], e["target_row_number"], e["target_field"]) for e in lineage["entries"]]
        self.assertEqual(keys, sorted(keys))

    def test_41_manifest_resource_sha(self):
        temp, output, _, _ = _build_temp()
        self.addCleanup(temp.cleanup)
        manifest = json.loads((output / "package_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["resources"][0]["content_sha256"], _sha(output / "customer.csv"))

    def test_42_manifest_lineage_sha(self):
        temp, output, _, _ = _build_temp()
        self.addCleanup(temp.cleanup)
        manifest = json.loads((output / "package_manifest.json").read_text(encoding="utf-8"))
        lineage = json.loads((output / "lineage.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["lineage"]["sha256"], lineage["_run_info"]["content_sha256"])

    def test_43_content_sha_stable(self):
        temp = tempfile.TemporaryDirectory(dir=GENERATED_ROOT)
        output = Path(temp.name) / "package"
        validation = Path(temp.name) / "validation.json"
        report_a = build_migration_package(_contract(), GENERIC_SOURCE, GENERIC_MAPPING, GENERIC_DECISIONS, output, validation_report_path=validation)
        report_b = build_migration_package(_contract(), GENERIC_SOURCE, GENERIC_MAPPING, GENERIC_DECISIONS, output, validation_report_path=validation)
        self.addCleanup(temp.cleanup)
        self.assertEqual(report_a["_run_info"]["content_sha256"], report_b["_run_info"]["content_sha256"])

    def test_44_source_change_changes_sha(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            source = Path(root) / "source.csv"
            source.write_bytes(GENERIC_SOURCE.read_bytes())
            report_json = json.loads(GENERIC_MAPPING.read_text(encoding="utf-8"))
            report_json["_meta"]["source_path"] = _rel(source)
            report_json["_meta"]["source_sha256"] = _sha(source)
            mapping = _write_mapping(Path(root), report_json)
            doc = _load_yaml()
            doc["mapping_report"] = _rel(mapping)
            doc["source"]["path"] = _rel(source)
            decisions = _write_yaml(Path(root), doc)
            temp_a, _, _, report_a = _build_temp(decisions=decisions, source=source, mapping=mapping)
            rows = source.read_text(encoding="utf-8").replace("Synthetic Aurora Stores", "Synthetic Changed Stores")
            source.write_text(rows, encoding="utf-8")
            report_json["_meta"]["source_sha256"] = _sha(source)
            mapping = _write_mapping(Path(root), report_json, "mapping2.json")
            doc["mapping_report"] = _rel(mapping)
            decisions = _write_yaml(Path(root), doc, "decisions2.yaml")
            temp_b, _, _, report_b = _build_temp(decisions=decisions, source=source, mapping=mapping)
            self.addCleanup(temp_a.cleanup)
            self.addCleanup(temp_b.cleanup)
            self.assertNotEqual(report_a["_run_info"]["content_sha256"], report_b["_run_info"]["content_sha256"])

    def test_45_decision_change_changes_sha(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_yaml()
            path_a = _write_yaml(Path(root), doc, "a.yaml")
            doc["decisions"][-1]["reason"] = "Rejected after review."
            path_b = _write_yaml(Path(root), doc, "b.yaml")
            temp_a, _, _, report_a = _build_temp(decisions=path_a)
            temp_b, _, _, report_b = _build_temp(decisions=path_b)
            self.addCleanup(temp_a.cleanup)
            self.addCleanup(temp_b.cleanup)
            self.assertNotEqual(report_a["_run_info"]["content_sha256"], report_b["_run_info"]["content_sha256"])

    def test_46_generic_generated_package_valid(self):
        temp, _, validation, _ = _build_temp()
        self.addCleanup(temp.cleanup)
        self.assertTrue(json.loads(validation.read_text(encoding="utf-8"))["summary"]["valid"])

    def test_47_sap_generated_package_valid(self):
        temp = tempfile.TemporaryDirectory(dir=GENERATED_ROOT)
        report = build_migration_package(_contract(SAP_CONTRACT, SAP_DATA), SAP_SOURCE, SAP_MAPPING, SAP_DECISIONS, Path(temp.name) / "sap", validation_report_path=Path(temp.name) / "validation.json")
        self.addCleanup(temp.cleanup)
        self.assertTrue(report["validation"]["valid"])

    def test_48_generic_findings_zero(self):
        temp, _, _, report = _build_temp()
        self.addCleanup(temp.cleanup)
        self.assertEqual(report["validation"]["finding_count"], 0)

    def test_49_sap_findings_zero(self):
        temp = tempfile.TemporaryDirectory(dir=GENERATED_ROOT)
        report = build_migration_package(_contract(SAP_CONTRACT, SAP_DATA), SAP_SOURCE, SAP_MAPPING, SAP_DECISIONS, Path(temp.name) / "sap", validation_report_path=Path(temp.name) / "validation.json")
        self.addCleanup(temp.cleanup)
        self.assertEqual(report["validation"]["finding_count"], 0)

    def test_50_smoke_passes(self):
        result = subprocess.run([sys.executable, "scripts/smoke_test_migration_package_generation.py"], cwd=PROJECT_ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Validation: valid", result.stdout)

    def test_51_cli_success_returns_zero(self):
        with tempfile.TemporaryDirectory(dir=GENERATED_ROOT) as root:
            result = subprocess.run(_cli_args("generic", Path(root) / "pkg", Path(root) / "build.json", Path(root) / "validation.json"), cwd=PROJECT_ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_52_generated_invalid_returns_one(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root, tempfile.TemporaryDirectory(dir=GENERATED_ROOT) as out:
            doc = _load_yaml()
            doc["decisions"][2]["transformation"] = {"type": "value_map", "values": {"DE": "DE", "GB": "GB"}, "on_missing": "reject_row"}
            decisions = _write_yaml(Path(root), doc)
            result = subprocess.run(_cli_args("generic", Path(out) / "pkg", Path(out) / "build.json", Path(out) / "validation.json", decisions=decisions), cwd=PROJECT_ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)

    def test_53_malformed_input_returns_two(self):
        with tempfile.TemporaryDirectory(dir=GENERATED_ROOT) as root:
            result = subprocess.run(_cli_args("generic", Path(root) / "pkg", Path(root) / "build.json", Path(root) / "validation.json", decisions=Path("missing.yaml")), cwd=PROJECT_ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 2)

    def test_54_unresolved_required_mappings_returns_three(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root, tempfile.TemporaryDirectory(dir=GENERATED_ROOT) as out:
            doc = _load_yaml()
            doc["decisions"][2]["decision"] = "deferred"
            doc["decisions"][2]["target"] = None
            decisions = _write_yaml(Path(root), doc)
            result = subprocess.run(_cli_args("generic", Path(out) / "pkg", Path(out) / "build.json", Path(out) / "validation.json", decisions=decisions), cwd=PROJECT_ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 3)


if __name__ == "__main__":
    unittest.main()
