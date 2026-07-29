from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]

from scripts.smoke_test_multitarget_package_generation import (
    BLIND_EVALUATION_SHA,
    BLIND_MAPPING_SHA,
    BUILD_REPORT,
    CONTRACT,
    CONTRACT_DATA,
    DECISIONS,
    GENERIC_BUILD,
    GENERIC_BUILD_SHA,
    GENERIC_MANIFEST,
    GENERIC_MANIFEST_SHA,
    LOCK,
    MAPPING,
    OUTPUT_ROOT,
    PROTOCOL_LOCK_SHA,
    REMEDIATION_REPORT,
    SAP_BUILD,
    SAP_BUILD_SHA,
    SAP_MANIFEST,
    SAP_MANIFEST_SHA,
    SOURCE,
    VALIDATION_REPORT,
    build_remediation_report,
    content_sha,
    main as smoke_main,
    sha256,
)
from src.core.contracts.loader import load_migration_contract
from src.core.package_generation.builder import build_migration_package
from src.core.package_generation.decision_loader import DecisionLoadError, load_mapping_decisions


GENERIC_CONTRACT = PROJECT_ROOT / "contracts" / "generic_customer" / "datapackage.yaml"
GENERIC_DATA = PROJECT_ROOT / "data" / "examples" / "generic_customer"
GENERIC_MAPPING = PROJECT_ROOT / "data" / "synthetic" / "generic_customer_contract_mapping.json"
GENERIC_DECISIONS = PROJECT_ROOT / "data" / "examples" / "package_generation" / "generic_customer" / "mapping_decisions.yaml"
SAP_CONTRACT = PROJECT_ROOT / "contracts" / "sap_supplier_reference" / "datapackage.yaml"
SAP_DATA = PROJECT_ROOT / "data" / "examples" / "sap_supplier_reference"
SAP_MAPPING = PROJECT_ROOT / "data" / "synthetic" / "sap_supplier_reference_contract_mapping.json"
SAP_DECISIONS = PROJECT_ROOT / "data" / "examples" / "package_generation" / "sap_supplier_reference" / "mapping_decisions.yaml"
GENERATED_ROOT = PROJECT_ROOT / "data" / "generated"


def _contract():
    return load_migration_contract(CONTRACT, CONTRACT_DATA)


def _load_doc() -> dict:
    return yaml.safe_load(DECISIONS.read_text(encoding="utf-8"))


def _write_doc(root: Path, doc: dict) -> Path:
    path = root / "mapping_decisions.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _load_decisions(path: Path = DECISIONS):
    return load_mapping_decisions(path, _contract(), MAPPING)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _source_rows() -> list[dict[str, str]]:
    return _read_csv(SOURCE)


def _lineage_entries() -> list[dict]:
    return _read_json(OUTPUT_ROOT / "lineage.json")["entries"]


def _target_cells() -> list[tuple[str, int, str]]:
    return [
        (entry["target_resource"], int(entry["target_row_number"]), entry["target_field"])
        for entry in _lineage_entries()
    ]


class MultitargetPackageGenerationTests(unittest.TestCase):
    def test_01_same_source_different_target_allowed(self):
        decisions = _load_decisions()
        targets = {item.target for item in decisions.approved() if item.source_field == "article_number"}
        self.assertEqual(targets, {"item.item_code", "item_price.item_code"})

    def test_02_same_source_same_target_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_doc()
            doc["decisions"].append(dict(doc["decisions"][0]))
            with self.assertRaises(DecisionLoadError) as ctx:
                _load_decisions(_write_doc(Path(root), doc))
        self.assertEqual(ctx.exception.code, "duplicate_approved_link")

    def test_03_different_source_same_target_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_doc()
            doc["decisions"][2]["target"] = "item.item_code"
            with self.assertRaises(DecisionLoadError) as ctx:
                _load_decisions(_write_doc(Path(root), doc))
        self.assertEqual(ctx.exception.code, "duplicate_approved_target")

    def test_04_approved_and_rejected_same_source_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_doc()
            doc["decisions"].append({"source_field": "article_number", "target": None, "decision": "rejected"})
            with self.assertRaises(DecisionLoadError) as ctx:
                _load_decisions(_write_doc(Path(root), doc))
        self.assertEqual(ctx.exception.code, "conflicting_source_decisions")

    def test_05_approved_and_deferred_same_source_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_doc()
            doc["decisions"].append({"source_field": "article_number", "target": None, "decision": "deferred"})
            with self.assertRaises(DecisionLoadError) as ctx:
                _load_decisions(_write_doc(Path(root), doc))
        self.assertEqual(ctx.exception.code, "conflicting_source_decisions")

    def test_06_rejected_and_deferred_same_source_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_doc()
            doc["decisions"].append({"source_field": "data_steward", "target": None, "decision": "deferred"})
            with self.assertRaises(DecisionLoadError) as ctx:
                _load_decisions(_write_doc(Path(root), doc))
        self.assertEqual(ctx.exception.code, "conflicting_source_decisions")

    def test_07_duplicate_rejected_source_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_doc()
            doc["decisions"].append(dict(doc["decisions"][-1]))
            with self.assertRaises(DecisionLoadError) as ctx:
                _load_decisions(_write_doc(Path(root), doc))
        self.assertEqual(ctx.exception.code, "duplicate_nonapproved_source_decision")

    def test_08_duplicate_deferred_source_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_doc()
            doc["decisions"][-1]["decision"] = "deferred"
            doc["decisions"].append(dict(doc["decisions"][-1]))
            with self.assertRaises(DecisionLoadError) as ctx:
                _load_decisions(_write_doc(Path(root), doc))
        self.assertEqual(ctx.exception.code, "duplicate_nonapproved_source_decision")

    def test_09_two_approved_links_are_both_top3(self):
        mapping = _read_json(MAPPING)
        article = next(item for item in mapping["mappings"] if item["source_field"] == "article_number")
        top3 = {candidate["target"] for candidate in article["top_candidates"][:3]}
        self.assertTrue({"item.item_code", "item_price.item_code"}.issubset(top3))

    def test_10_second_target_outside_top3_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_doc()
            doc["decisions"][1]["target"] = "item_price.price_list_rate"
            with self.assertRaises(DecisionLoadError) as ctx:
                _load_decisions(_write_doc(Path(root), doc))
        self.assertEqual(ctx.exception.code, "target_not_in_top3")

    def test_11_transformations_are_link_specific(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            doc = _load_doc()
            doc["decisions"][1]["transformation"] = {"type": "constant", "value": "CONST"}
            decisions = _load_decisions(_write_doc(Path(root), doc))
        by_target = {item.target: item.transformation.type for item in decisions.approved() if item.source_field == "article_number"}
        self.assertEqual(by_target, {"item.item_code": "copy", "item_price.item_code": "constant"})

    def test_12_old_generic_decision_remains_valid(self):
        contract = load_migration_contract(GENERIC_CONTRACT, GENERIC_DATA)
        decisions = load_mapping_decisions(GENERIC_DECISIONS, contract, GENERIC_MAPPING)
        self.assertEqual(len(decisions.approved()), 11)

    def test_13_old_sap_decision_remains_valid(self):
        contract = load_migration_contract(SAP_CONTRACT, SAP_DATA)
        decisions = load_mapping_decisions(SAP_DECISIONS, contract, SAP_MAPPING)
        self.assertEqual(len(decisions.rejected()), 1)

    def test_14_article_number_writes_item_code(self):
        source = _source_rows()
        rows = _read_csv(OUTPUT_ROOT / "item.csv")
        self.assertEqual([row["item_code"] for row in rows], [row["article_number"] for row in source])

    def test_15_article_number_writes_item_price_code(self):
        source = _source_rows()
        rows = _read_csv(OUTPUT_ROOT / "item_price.csv")
        self.assertEqual([row["item_code"] for row in rows], [row["article_number"] for row in source])

    def test_16_inventory_measure_writes_item_stock_uom(self):
        source = _source_rows()
        rows = _read_csv(OUTPUT_ROOT / "item.csv")
        self.assertEqual([row["stock_uom"] for row in rows], [row["inventory_measure"] for row in source])

    def test_17_inventory_measure_writes_item_price_uom(self):
        source = _source_rows()
        rows = _read_csv(OUTPUT_ROOT / "item_price.csv")
        self.assertEqual([row["uom"] for row in rows], [row["inventory_measure"] for row in source])

    def test_18_item_rows_are_eight(self):
        self.assertEqual(len(_read_csv(OUTPUT_ROOT / "item.csv")), 8)

    def test_19_item_price_rows_are_eight(self):
        self.assertEqual(len(_read_csv(OUTPUT_ROOT / "item_price.csv")), 8)

    def test_20_target_columns_follow_contract(self):
        self.assertEqual(list(_read_csv(OUTPUT_ROOT / "item.csv")[0]), ["item_code", "item_name", "item_group", "stock_uom", "disabled"])
        self.assertEqual(list(_read_csv(OUTPUT_ROOT / "item_price.csv")[0]), ["item_code", "uom", "price_list", "price_list_rate", "valid_from", "valid_upto"])

    def test_21_generated_package_valid(self):
        self.assertTrue(_read_json(VALIDATION_REPORT)["summary"]["valid"])

    def test_22_finding_count_zero(self):
        self.assertEqual(_read_json(VALIDATION_REPORT)["summary"]["finding_count"], 0)

    def test_23_foreign_key_valid(self):
        item_codes = {row["item_code"] for row in _read_csv(OUTPUT_ROOT / "item.csv")}
        price_codes = {row["item_code"] for row in _read_csv(OUTPUT_ROOT / "item_price.csv")}
        self.assertTrue(price_codes.issubset(item_codes))

    def test_24_rejected_source_field_not_output(self):
        output_text = (OUTPUT_ROOT / "item.csv").read_text(encoding="utf-8") + (OUTPUT_ROOT / "item_price.csv").read_text(encoding="utf-8")
        self.assertNotIn("data_steward", output_text)
        self.assertNotIn("steward_alpha", output_text)

    def test_25_input_unchanged(self):
        lock = _read_json(LOCK)
        self.assertEqual(sha256(SOURCE), lock["source_sha256"])

    def test_26_atomic_output_still_valid(self):
        with tempfile.TemporaryDirectory(dir=GENERATED_ROOT) as root:
            report = build_migration_package(
                _contract(),
                SOURCE,
                MAPPING,
                DECISIONS,
                Path(root) / "package",
                validation_report_path=Path(root) / "validation.json",
            )
        self.assertTrue(report["validation"]["valid"])

    def test_27_lineage_entries_are_eighty_eight(self):
        self.assertEqual(len(_lineage_entries()), 88)

    def test_28_article_number_lineage_is_sixteen(self):
        self.assertEqual(sum(1 for entry in _lineage_entries() if entry["source_field"] == "article_number"), 16)

    def test_29_inventory_measure_lineage_is_sixteen(self):
        self.assertEqual(sum(1 for entry in _lineage_entries() if entry["source_field"] == "inventory_measure"), 16)

    def test_30_no_duplicate_target_cells(self):
        cells = _target_cells()
        self.assertEqual(len(cells), len(set(cells)))

    def test_31_every_nonempty_target_cell_has_lineage(self):
        lineage_cells = set(_target_cells())
        for resource, filename in (("item", "item.csv"), ("item_price", "item_price.csv")):
            for row_number, row in enumerate(_read_csv(OUTPUT_ROOT / filename), start=1):
                for field, value in row.items():
                    if value:
                        self.assertIn((resource, row_number, field), lineage_cells)

    def test_32_lineage_has_hashes_not_raw_source_values(self):
        text = (OUTPUT_ROOT / "lineage.json").read_text(encoding="utf-8")
        self.assertNotIn("Synthetic Atlas Gear", text)
        self.assertNotIn("Standard Selling", text)
        source_value_key = "source_" + "value"
        self.assertTrue(all("source_value_sha256" in entry and source_value_key not in entry for entry in _lineage_entries()))

    def test_33_source_hashes_are_valid(self):
        entry = next(item for item in _lineage_entries() if item["source_field"] == "article_number" and item["target_resource"] == "item")
        self.assertEqual(entry["source_value_sha256"], hashlib.sha256("ART-3001".encode("utf-8")).hexdigest())

    def test_34_lineage_ordering_is_deterministic(self):
        cells = _target_cells()
        self.assertEqual(cells, sorted(cells))

    def test_35_blind_mapping_sha_unchanged(self):
        self.assertEqual(content_sha(MAPPING), BLIND_MAPPING_SHA)

    def test_36_blind_evaluation_sha_unchanged(self):
        self.assertEqual(content_sha(PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_evaluation.json"), BLIND_EVALUATION_SHA)

    def test_37_protocol_lock_sha_unchanged(self):
        self.assertEqual(sha256(LOCK), PROTOCOL_LOCK_SHA)

    def test_38_engine_file_sha_unchanged(self):
        lock = _read_json(LOCK)
        self.assertTrue(all(sha256(PROJECT_ROOT / path) == expected for path, expected in lock["engine_files"].items()))

    def test_39_generic_manifest_sha_unchanged(self):
        self.assertEqual(content_sha(GENERIC_MANIFEST), GENERIC_MANIFEST_SHA)

    def test_40_sap_manifest_sha_unchanged(self):
        self.assertEqual(content_sha(SAP_MANIFEST), SAP_MANIFEST_SHA)
        self.assertEqual(content_sha(GENERIC_BUILD), GENERIC_BUILD_SHA)
        self.assertEqual(content_sha(SAP_BUILD), SAP_BUILD_SHA)

    def test_41_remediation_report_stable(self):
        self.assertEqual(build_remediation_report()["_run_info"]["content_sha256"], content_sha(REMEDIATION_REPORT))

    def test_42_smoke_passes(self):
        self.assertEqual(smoke_main(), 0)


if __name__ == "__main__":
    unittest.main()
