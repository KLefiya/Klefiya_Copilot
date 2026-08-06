from __future__ import annotations

import csv
import math
import json
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

from src.core.hashing import canonical_json_content_sha256, normalized_text_sha256, provenance_text_or_raw_sha256
from src.core.contracts.loader import load_migration_contract
from src.core.mapping.engine import suggest_contract_mappings, write_mapping_report
from src.core.mapping.profiler import profile_source_csv
from src.core.mapping.protocol_lock import (
    ProtocolLockError,
    validate_effective_protocol_lock,
    validate_historical_protocol_lock,
)
from src.tools.evaluate_blind_multitarget_mapping import evaluate_blind_multitarget_mapping


REFERENCE = PROJECT_ROOT / "references" / "erpnext_item_price" / "upstream_reference.json"
CONTRACT = PROJECT_ROOT / "contracts" / "erpnext_item_price_reference" / "datapackage.yaml"
DATA_ROOT = PROJECT_ROOT / "data" / "examples" / "blind" / "erpnext_item_price"
SOURCE = DATA_ROOT / "source_product_catalog.csv"
TRUTH = DATA_ROOT / "ground_truth.json"
LOCK = DATA_ROOT / "blind_protocol_lock.json"
AMENDMENT = DATA_ROOT / "blind_protocol_compatibility_amendment_v1.json"
MAPPING = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_mapping.json"
EVALUATION = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_evaluation.json"
PROTECTED_FILES = (REFERENCE, CONTRACT, SOURCE, TRUTH, LOCK, AMENDMENT, MAPPING, EVALUATION)


def _sha(path: Path) -> str:
    return provenance_text_or_raw_sha256(path)


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _source_rows() -> list[dict[str, str]]:
    with SOURCE.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _contract_doc():
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def _aliases_count() -> int:
    return CONTRACT.read_text(encoding="utf-8").count("aliases:")


def _per_source(name: str) -> dict:
    evaluation = _json(EVALUATION)
    return next(item for item in evaluation["per_source_results"] if item["source_field"] == name)


def _has_absolute_path(value) -> bool:
    if isinstance(value, dict):
        return any(_has_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_absolute_path(item) for item in value)
    if isinstance(value, str):
        return bool(re.match(r"^[A-Za-z]:[\\/]", value))
    return False


def _set_content_sha(report: dict) -> dict:
    report["_run_info"] = {"content_sha256": canonical_json_content_sha256(report)}
    return report


def _write_json(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


class FakeEmbeddingBackend:
    def encode(self, sentences, normalize_embeddings=True, show_progress_bar=False):
        return [self._vector(text, normalize_embeddings) for text in sentences]

    def _vector(self, text: str, normalize_embeddings: bool):
        text = text.lower()
        features = [
            "article",
            "name",
            "group",
            "measure",
            "price",
            "currency",
            "valid",
            "lifecycle",
            "steward",
            "item",
            "uom",
        ]
        vector = [1.0 if feature in text else 0.0 for feature in features]
        vector.append(0.0 if any(vector) else 1.0)
        if normalize_embeddings:
            norm = math.sqrt(sum(item * item for item in vector))
            vector = [item / norm for item in vector]
        return vector


class ErpnextBlindMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._protected_bytes = {path: path.read_bytes() for path in PROTECTED_FILES}

    def tearDown(self) -> None:
        for path, before in self._protected_bytes.items():
            self.assertEqual(path.read_bytes(), before, f"{path} was modified")

    def test_01_upstream_reference_has_fixed_commit(self):
        reference = _json(REFERENCE)
        self.assertEqual(reference["repository"], "frappe/erpnext")
        self.assertEqual(reference["commit_sha"], "a051a12d9b6603bb838f8b8149436a3a7900d545")
        self.assertEqual(reference["license"], "GPL-3.0")

    def test_02_upstream_sources_have_sha(self):
        reference = _json(REFERENCE)
        self.assertEqual(len(reference["sources"]), 2)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in reference["sources"]))

    def test_03_contract_is_not_authoritative(self):
        doc = _contract_doc()
        self.assertFalse(doc["carveops"]["authoritative"])
        self.assertEqual(doc["carveops"]["contract_id"], "erpnext-item-price-reference-v1")

    def test_04_contract_has_zero_aliases(self):
        self.assertEqual(_aliases_count(), 0)

    def test_05_source_does_not_use_target_field_names(self):
        header = list(_source_rows()[0].keys())
        forbidden = {"item_code", "item_name", "item_group", "stock_uom", "disabled", "uom", "price_list", "price_list_rate", "valid_from", "valid_upto"}
        self.assertFalse(forbidden.intersection(header))

    def test_06_source_has_ten_fields_and_eight_rows(self):
        rows = _source_rows()
        self.assertEqual(len(rows), 8)
        self.assertEqual(len(rows[0]), 10)

    def test_07_ground_truth_stats(self):
        truth = _json(TRUTH)
        links = sum(len(item["expected_targets"]) for item in truth["mappings"])
        multi = sum(1 for item in truth["mappings"] if len(item["expected_targets"]) > 1)
        no_target = sum(1 for item in truth["mappings"] if not item["expected_targets"])
        self.assertEqual(links, 11)
        self.assertEqual(multi, 2)
        self.assertEqual(no_target, 1)

    def test_08_lock_was_created_before_mapping(self):
        lock = _json(LOCK)
        self.assertTrue(lock["locked_before_first_mapping"])
        self.assertFalse(lock["aliases_present"])

    def test_09_lock_sha_matches_files(self):
        lock = _json(LOCK)
        self.assertEqual(lock["contract_sha256"], _sha(CONTRACT))
        self.assertEqual(lock["source_sha256"], _sha(SOURCE))
        self.assertEqual(lock["ground_truth_sha256"], _sha(TRUTH))
        self.assertEqual(lock["upstream_reference_sha256"], _sha(REFERENCE))

    def test_10_engine_commit_is_correct(self):
        self.assertEqual(_json(LOCK)["engine_commit"], "c908712daab4f4989988d41e869df693b5d8944a")

    def test_11_engine_file_sha_matches_lock(self):
        with self.assertRaises(ProtocolLockError) as ctx:
            validate_historical_protocol_lock(LOCK)
        self.assertEqual(ctx.exception.code, "hash_mismatch")
        changed = ctx.exception.details["changed_engine_files"]
        self.assertEqual([item["path"] for item in changed], ["src/core/mapping/engine.py", "src/core/mapping/profiler.py"])
        result = validate_effective_protocol_lock(LOCK, AMENDMENT)
        self.assertEqual(result["validation"], "valid")
        self.assertEqual(result["protocol_amendment_content_sha256"], _json(AMENDMENT)["_run_info"]["content_sha256"])
        self.assertEqual(
            [item["path"] for item in result["allowed_engine_file_changes"]],
            ["src/core/mapping/engine.py", "src/core/mapping/profiler.py"],
        )

    def test_12_mapping_engine_does_not_read_truth_file(self):
        files = [
            PROJECT_ROOT / "src/core/mapping/profiler.py",
            PROJECT_ROOT / "src/core/mapping/target_index.py",
            PROJECT_ROOT / "src/core/mapping/scorer.py",
            PROJECT_ROOT / "src/core/mapping/engine.py",
            PROJECT_ROOT / "src/tools/suggest_contract_mappings.py",
        ]
        forbidden = ("ground_truth.json", "ground_truth_path", "ground-truth", "Ground Truth")
        self.assertFalse(any(pattern in path.read_text(encoding="utf-8") for path in files for pattern in forbidden))

    def test_13_evaluator_does_not_modify_mapping(self):
        before = _sha(MAPPING)
        evaluate_blind_multitarget_mapping(MAPPING, TRUTH, LOCK, AMENDMENT)
        self.assertEqual(before, _sha(MAPPING))

    def test_14_multitarget_metrics_are_correct(self):
        summary = _json(EVALUATION)["summary"]
        self.assertEqual(summary["expected_target_links"], 11)
        self.assertEqual(summary["top3_target_links_found"], 11)
        self.assertEqual(summary["top3_target_link_recall"], 1.0)
        self.assertEqual(summary["multi_target_full_top3_coverage"], 1.0)

    def test_15_no_target_metrics_are_correct(self):
        summary = _json(EVALUATION)["summary"]
        self.assertEqual(summary["no_target_source_fields"], 1)
        self.assertEqual(summary["no_target_correct"], 1)
        self.assertEqual(summary["false_positive_no_target"], 0)

    def test_16_zero_high_confidence_precision_is_undefined(self):
        summary = _json(EVALUATION)["summary"]
        self.assertEqual(summary["high_confidence_predictions"], 0)
        self.assertEqual(summary["high_confidence_source_correct"], 0)
        self.assertIsNone(summary["high_confidence_source_precision"])
        self.assertFalse(summary["high_confidence_source_precision_defined"])

    def test_17_positive_high_confidence_precision_is_defined(self):
        mapping = _json(MAPPING)
        for item in mapping["mappings"]:
            if item["source_field"] == "inventory_measure":
                item["confidence"] = 0.9
                item["recommendation"] = "item.stock_uom"
                break
        mapping["_run_info"]["content_sha256"] = canonical_json_content_sha256(mapping)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            mapping_path = Path(tmp) / "mapping.json"
            mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
            report = evaluate_blind_multitarget_mapping(mapping_path, TRUTH, LOCK, AMENDMENT)
        summary = report["summary"]
        self.assertEqual(summary["high_confidence_predictions"], 1)
        self.assertEqual(summary["high_confidence_source_correct"], 1)
        self.assertEqual(summary["high_confidence_source_precision"], 1.0)
        self.assertTrue(summary["high_confidence_source_precision_defined"])

    def test_18_capability_observations_are_current_boundaries(self):
        observations = _json(EVALUATION)["capability_observations"]
        self.assertFalse(observations["mapping_report_supports_multiple_recommendations_per_source"])
        self.assertFalse(observations["decision_loader_supports_one_source_to_multiple_targets"])
        self.assertFalse(observations["package_builder_can_execute_one_to_many_source_mapping"])

    def test_19_reports_have_no_absolute_paths(self):
        self.assertFalse(_has_absolute_path(_json(MAPPING)))
        self.assertFalse(_has_absolute_path(_json(EVALUATION)))

    def test_20_evaluation_content_sha_is_stable(self):
        first = evaluate_blind_multitarget_mapping(MAPPING, TRUTH, LOCK, AMENDMENT)
        second = evaluate_blind_multitarget_mapping(MAPPING, TRUTH, LOCK, AMENDMENT)
        self.assertEqual(first["_run_info"]["content_sha256"], second["_run_info"]["content_sha256"])

    def test_21_article_number_result_is_preserved(self):
        result = _per_source("article_number")
        self.assertIsNone(result["recommendation"])
        self.assertEqual(result["expected_targets_found_in_top3"], ["item.item_code", "item_price.item_code"])

    def test_22_inventory_measure_result_is_preserved(self):
        result = _per_source("inventory_measure")
        self.assertEqual(result["recommendation"], "item.stock_uom")
        self.assertEqual(result["expected_targets_found_in_top3"], ["item.stock_uom", "item_price.uom"])

    def test_23_data_steward_result_is_preserved(self):
        result = _per_source("data_steward")
        self.assertIsNone(result["recommendation"])
        self.assertEqual(result["expected_targets"], [])

    def test_24_mapping_sha_is_preserved(self):
        self.assertEqual(
            _json(MAPPING)["_run_info"]["content_sha256"],
            "99007ad5da580b6e764b01e3a9739840bcfcff1b1a16c29cf708124ebbc56703",
        )

    def test_25_protocol_lock_file_sha_is_preserved(self):
        self.assertEqual(
            _sha(LOCK),
            "bd092f06592d6a71961454cf638e2864ac3e5fb8fc0f247a1fe0b8ae36fdb2ed",
        )

    def test_26_substantive_metrics_are_preserved(self):
        summary = _json(EVALUATION)["summary"]
        self.assertEqual(summary["source_top1_accuracy"], 0.2222)
        self.assertEqual(summary["top3_target_link_recall"], 1.0)
        self.assertEqual(summary["multi_target_full_top3_coverage"], 1.0)
        self.assertEqual(summary["no_target_accuracy"], 1.0)

    def test_27_smoke_passes(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            output_root = Path(root) / "outputs with space"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/smoke_test_erpnext_blind_mapping.py",
                    "--output-root",
                    str(output_root),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Validation: valid", result.stdout)

    def test_28_text_sha_normalizes_line_endings(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            temp = Path(root)
            lf = temp / "lf.json"
            crlf = temp / "crlf.json"
            cr = temp / "cr.json"
            lf.write_bytes(b'{\n  "a": 1\n}\n')
            crlf.write_bytes(b'{\r\n  "a": 1\r\n}\r\n')
            cr.write_bytes(b'{\r  "a": 1\r}\r')
            self.assertEqual(normalized_text_sha256(lf), normalized_text_sha256(crlf))
            self.assertEqual(normalized_text_sha256(lf), normalized_text_sha256(cr))

    def test_29_text_sha_changes_for_real_content_change(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            temp = Path(root)
            first = temp / "first.py"
            second = temp / "second.py"
            first.write_text("value = 1\n", encoding="utf-8")
            second.write_text("value = 2\n", encoding="utf-8")
            self.assertNotEqual(normalized_text_sha256(first), normalized_text_sha256(second))

    def test_30_source_csv_sha_normalizes_line_endings(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            source = Path(root) / "source.csv"
            base = SOURCE.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
            shas = []
            for newline in ("\n", "\r\n", "\r"):
                source.write_bytes(base.replace("\n", newline).encode("utf-8"))
                _, meta = profile_source_csv(source)
                shas.append(meta["source_sha256"])
                self.assertEqual(meta["source_hash_mode"], "normalized_text_sha256_v1")
            self.assertEqual(shas[0], shas[1])
            self.assertEqual(shas[0], shas[2])

    def test_31_source_csv_content_change_changes_profile_sha(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            source = Path(root) / "source.csv"
            source.write_bytes(SOURCE.read_bytes())
            _, first_meta = profile_source_csv(source)
            source.write_text(source.read_text(encoding="utf-8").replace("ART-3001", "ART-CHANGED"), encoding="utf-8")
            _, second_meta = profile_source_csv(source)
            self.assertNotEqual(first_meta["source_sha256"], second_meta["source_sha256"])

    def test_32_mapping_report_content_sha_normalizes_source_line_endings(self):
        contract = load_migration_contract(CONTRACT, DATA_ROOT)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            source = Path(root) / "source.csv"
            output = Path(root) / "mapping.json"
            base = SOURCE.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
            content_shas = []
            source_shas = []
            source_hash_modes = []
            mappings = []
            for newline in ("\n", "\r\n", "\r"):
                source.write_bytes(base.replace("\n", newline).encode("utf-8"))
                report = suggest_contract_mappings(contract, source, embedding_backend=FakeEmbeddingBackend())
                write_mapping_report(report, output)
                written = json.loads(output.read_text(encoding="utf-8"))
                source_shas.append(written["_meta"]["source_sha256"])
                source_hash_modes.append(written["_meta"]["source_hash_mode"])
                mappings.append(written["mappings"])
                content_shas.append(written["_run_info"]["content_sha256"])
            self.assertEqual(source_shas[0], source_shas[1])
            self.assertEqual(source_shas[0], source_shas[2])
            self.assertEqual(source_hash_modes, ["normalized_text_sha256_v1"] * 3)
            self.assertEqual(mappings[0], mappings[1])
            self.assertEqual(mappings[0], mappings[2])
            self.assertEqual(content_shas[0], content_shas[1])
            self.assertEqual(content_shas[0], content_shas[2])

    def test_33_effective_lock_rejects_missing_amendment_for_current_profiler(self):
        with self.assertRaisesRegex(ProtocolLockError, "engine:src/core/mapping/profiler.py"):
            validate_historical_protocol_lock(LOCK)
        try:
            validate_historical_protocol_lock(LOCK)
        except ProtocolLockError as exc:
            self.assertEqual(
                [item["path"] for item in exc.details["changed_engine_files"]],
                ["src/core/mapping/engine.py", "src/core/mapping/profiler.py"],
            )

    def test_34_effective_lock_rejects_non_profiler_engine_change_amendment(self):
        amendment = _json(AMENDMENT)
        amendment["allowed_engine_file_changes"][0]["path"] = "src/core/mapping/scorer.py"
        _set_content_sha(amendment)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            path = _write_json(Path(root) / "bad_amendment.json", amendment)
            with self.assertRaises(ProtocolLockError) as ctx:
                validate_effective_protocol_lock(LOCK, path)
        self.assertEqual(ctx.exception.code, "unexpected_engine_change_path")

    def test_34a_effective_lock_rejects_missing_allowed_engine_change(self):
        amendment = _json(AMENDMENT)
        amendment["allowed_engine_file_changes"] = amendment["allowed_engine_file_changes"][:1]
        _set_content_sha(amendment)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            path = _write_json(Path(root) / "missing_engine_amendment.json", amendment)
            with self.assertRaises(ProtocolLockError) as ctx:
                validate_effective_protocol_lock(LOCK, path)
        self.assertEqual(ctx.exception.code, "unexpected_engine_change_path")

    def test_34b_effective_lock_rejects_unapproved_third_engine_change(self):
        amendment = _json(AMENDMENT)
        amendment["allowed_engine_file_changes"].append(
            {
                "path": "src/core/mapping/target_index.py",
                "before_normalized_text_sha256": "a0ea3897a4fd49f502e641ddd8bb8ed2f6bc05690780669c255e552d5d4f0c72",
                "after_normalized_text_sha256": "a0ea3897a4fd49f502e641ddd8bb8ed2f6bc05690780669c255e552d5d4f0c72",
                "change_class": "provenance_only",
                "reason": "not permitted",
            }
        )
        _set_content_sha(amendment)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            path = _write_json(Path(root) / "extra_engine_amendment.json", amendment)
            with self.assertRaises(ProtocolLockError) as ctx:
                validate_effective_protocol_lock(LOCK, path)
        self.assertEqual(ctx.exception.code, "unexpected_engine_change_path")

    def test_34c_effective_lock_rejects_engine_after_sha_mismatch(self):
        amendment = _json(AMENDMENT)
        engine_change = next(item for item in amendment["allowed_engine_file_changes"] if item["path"] == "src/core/mapping/engine.py")
        engine_change["after_normalized_text_sha256"] = "0" * 64
        _set_content_sha(amendment)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            path = _write_json(Path(root) / "bad_engine_sha.json", amendment)
            with self.assertRaises(ProtocolLockError) as ctx:
                validate_effective_protocol_lock(LOCK, path)
        self.assertEqual(ctx.exception.code, "engine_after_sha_mismatch")

    def test_35_effective_lock_rejects_threshold_change_amendment(self):
        amendment = _json(AMENDMENT)
        amendment["unchanged_protocol_values"]["thresholds"]["top_n_candidates"] = 4
        _set_content_sha(amendment)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            path = _write_json(Path(root) / "bad_threshold.json", amendment)
            with self.assertRaises(ProtocolLockError) as ctx:
                validate_effective_protocol_lock(LOCK, path)
        self.assertEqual(ctx.exception.code, "unchanged_thresholds_mismatch")

    def test_36_effective_lock_rejects_amendment_content_tamper(self):
        amendment = _json(AMENDMENT)
        amendment["purpose"] = "changed"
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            path = _write_json(Path(root) / "tampered.json", amendment)
            with self.assertRaises(ProtocolLockError) as ctx:
                validate_effective_protocol_lock(LOCK, path)
        self.assertEqual(ctx.exception.code, "amendment_content_sha_mismatch")

    def test_37_effective_lock_rejects_wrong_base_lock_sha(self):
        amendment = _json(AMENDMENT)
        amendment["base_lock_normalized_text_sha256"] = "0" * 64
        _set_content_sha(amendment)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            path = _write_json(Path(root) / "bad_base.json", amendment)
            with self.assertRaises(ProtocolLockError) as ctx:
                validate_effective_protocol_lock(LOCK, path)
        self.assertEqual(ctx.exception.code, "base_lock_sha_mismatch")

    def test_38_maintenance_replay_preserves_business_outputs(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
            mapping_path = Path(root) / "mapping.json"
            evaluation_path = Path(root) / "evaluation.json"
            subprocess.run(
                [
                    sys.executable,
                    "src/tools/suggest_contract_mappings.py",
                    "--contract", str(CONTRACT),
                    "--data-root", str(DATA_ROOT),
                    "--source", str(SOURCE),
                    "--output", str(mapping_path),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    "src/tools/evaluate_blind_multitarget_mapping.py",
                    "--mapping-report", str(mapping_path),
                    "--ground-truth", str(TRUTH),
                    "--protocol-lock", str(LOCK),
                    "--protocol-amendment", str(AMENDMENT),
                    "--output", str(evaluation_path),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            replay_mapping = _json(mapping_path)
            replay_evaluation = _json(evaluation_path)
        formal_mapping = _json(MAPPING)
        formal_evaluation = _json(EVALUATION)
        for replay, formal in zip(replay_mapping["mappings"], formal_mapping["mappings"]):
            self.assertEqual(replay["source_field"], formal["source_field"])
            self.assertEqual(replay["recommendation"], formal["recommendation"])
            self.assertEqual(replay["top_candidates"], formal["top_candidates"])
            self.assertEqual(replay["confidence"], formal["confidence"])
            self.assertEqual(replay["status"], formal["status"])
        self.assertEqual(replay_evaluation["summary"], formal_evaluation["summary"])
        self.assertEqual(replay_evaluation["mapping_status_distribution"], formal_evaluation["mapping_status_distribution"])

    def test_39_formal_blind_mapping_records_source_hash_mode(self):
        formal_mapping = _json(MAPPING)
        self.assertEqual(formal_mapping["_meta"]["source_sha256"], normalized_text_sha256(SOURCE))
        self.assertEqual(formal_mapping["_meta"]["source_hash_mode"], "normalized_text_sha256_v1")
        self.assertEqual(formal_mapping["_run_info"]["content_sha256"], canonical_json_content_sha256(formal_mapping))
        self.assertFalse(_has_absolute_path(formal_mapping))


if __name__ == "__main__":
    unittest.main()
