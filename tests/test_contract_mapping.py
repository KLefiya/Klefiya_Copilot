from __future__ import annotations

import csv
import hashlib
import json
import math
import os
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
from src.core.mapping.engine import suggest_contract_mappings, write_mapping_report
from src.core.mapping.evaluator import evaluate_mapping_report
from src.core.mapping.profiler import SourceProfileError, profile_source_csv
from src.core.mapping.runtime import (
    BASELINE_SCORER_ID,
    SUPPORTED_RUNTIME_SCORERS,
    RuntimeScorerError,
    suggest_runtime_contract_mappings,
)
from src.core.mapping.scorer import (
    HIGH_CONFIDENCE,
    _type_gate,
    score_source_field,
)
from src.core.mapping.scorer_v4 import SCORER_ID as PRECISION_TIERED_V4_SCORER_ID
from src.core.mapping.scorer_v4 import suggest_contract_mappings_v4
from src.core.mapping.target_index import build_target_field_index
from src.tools.suggest_contract_mappings import build_parser


GENERIC_CONTRACT = PROJECT_ROOT / "contracts" / "generic_customer" / "datapackage.yaml"
GENERIC_DATA = PROJECT_ROOT / "data" / "examples" / "generic_customer"
GENERIC_SOURCE = PROJECT_ROOT / "data" / "examples" / "mapping" / "generic_customer" / "source_customer.csv"
GENERIC_TRUTH = PROJECT_ROOT / "data" / "examples" / "mapping" / "generic_customer" / "ground_truth.json"
SUPPLIER_CONTRACT = PROJECT_ROOT / "contracts" / "sap_supplier_reference" / "datapackage.yaml"
SUPPLIER_DATA = PROJECT_ROOT / "data" / "examples" / "sap_supplier_reference"
SUPPLIER_SOURCE = PROJECT_ROOT / "data" / "examples" / "mapping" / "sap_supplier_reference" / "source_supplier.csv"
SUPPLIER_TRUTH = PROJECT_ROOT / "data" / "examples" / "mapping" / "sap_supplier_reference" / "ground_truth.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeEmbeddingBackend:
    def encode(self, sentences, normalize_embeddings=True, show_progress_bar=False):
        return [self._vector(text, normalize_embeddings) for text in sentences]

    def _vector(self, text: str, normalize_embeddings: bool):
        text = text.lower()
        features = [
            "customer",
            "supplier",
            "name",
            "country",
            "phone",
            "tax",
            "payment",
            "bank",
            "currency",
            "identifier",
            "company",
            "language",
            "category",
            "account",
            "email",
        ]
        vector = [1.0 if feature in text else 0.0 for feature in features]
        if not any(vector):
            vector.append(1.0)
        else:
            vector.append(0.0)
        if normalize_embeddings:
            norm = math.sqrt(sum(item * item for item in vector))
            vector = [item / norm for item in vector]
        return vector


def _contract(path=GENERIC_CONTRACT, data=GENERIC_DATA):
    return load_migration_contract(path, data)


def _generic_report():
    return suggest_contract_mappings(
        _contract(),
        GENERIC_SOURCE,
        embedding_backend=FakeEmbeddingBackend(),
    )


class ContractMappingTests(unittest.TestCase):
    def test_01_generic_target_field_count(self):
        self.assertEqual(len(build_target_field_index(_contract())), 11)

    def test_02_supplier_target_field_count(self):
        self.assertEqual(len(build_target_field_index(_contract(SUPPLIER_CONTRACT, SUPPLIER_DATA))), 11)

    def test_03_qualified_name(self):
        fields = build_target_field_index(_contract())
        self.assertIn("customer.customer_id", [field.qualified_name for field in fields])

    def test_04_field_metadata(self):
        field = next(field for field in build_target_field_index(_contract()) if field.qualified_name == "customer.email")
        self.assertEqual(field.semantic_type, "email")
        self.assertIn("contact_email", field.aliases)
        self.assertIn("Email", field.description)

    def test_05_primary_key(self):
        field = next(field for field in build_target_field_index(_contract()) if field.qualified_name == "customer.customer_id")
        self.assertTrue(field.primary_key)

    def test_06_constraints(self):
        field = next(field for field in build_target_field_index(_contract()) if field.qualified_name == "customer.payment_terms")
        self.assertTrue(field.required)
        self.assertEqual(field.enum_values, ("NET15", "NET30", "NET45", "PREPAID"))

    def test_07_missing_field_carveops_safe_defaults(self):
        descriptor = yaml.safe_load(GENERIC_CONTRACT.read_text(encoding="utf-8"))
        descriptor["resources"][0]["schema"]["fields"][0].pop("carveops")
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            root = Path(temp_dir)
            data = root / "data"
            data.mkdir()
            for csv_file in GENERIC_DATA.glob("*.csv"):
                (data / csv_file.name).write_bytes(csv_file.read_bytes())
            contract_path = root / "datapackage.yaml"
            contract_path.write_text(yaml.safe_dump(descriptor), encoding="utf-8")
            field = build_target_field_index(load_migration_contract(contract_path, data))[0]
        self.assertEqual(field.description, "")
        self.assertEqual(field.aliases, ())
        self.assertEqual(field.semantic_type, "")

    def test_08_source_row_count(self):
        profiles, meta = profile_source_csv(GENERIC_SOURCE)
        self.assertEqual(meta["source_row_count"], 6)
        self.assertEqual(len(profiles), 12)

    def test_09_field_order(self):
        profiles, _ = profile_source_csv(GENERIC_SOURCE)
        self.assertEqual(profiles[0].name, "legacy_client_id")
        self.assertEqual(profiles[-1].name, "marketing_opt_in")

    def test_10_missing_ratio(self):
        profiles, _ = profile_source_csv(GENERIC_SOURCE)
        self.assertEqual(profiles[0].missing_ratio, 0.0)

    def test_11_distinct_ratio(self):
        profiles, _ = profile_source_csv(GENERIC_SOURCE)
        self.assertEqual(profiles[0].distinct_ratio, 1.0)

    def test_12_max_length(self):
        profiles, _ = profile_source_csv(GENERIC_SOURCE)
        phone = next(profile for profile in profiles if profile.name == "telephone_number")
        self.assertGreaterEqual(phone.observed_max_length, 7)

    def test_13_inferred_kind(self):
        profiles, _ = profile_source_csv(GENERIC_SOURCE)
        marketing = next(profile for profile in profiles if profile.name == "marketing_opt_in")
        self.assertEqual(marketing.inferred_kind, "boolean")

    def test_14_duplicate_columns_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            path = Path(temp_dir) / "dup.csv"
            path.write_text("a,a\n1,2\n", encoding="utf-8")
            with self.assertRaises(SourceProfileError) as ctx:
                profile_source_csv(path)
        self.assertEqual(ctx.exception.code, "duplicate_columns")

    def test_15_empty_source_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            path = Path(temp_dir) / "empty.csv"
            path.write_text("", encoding="utf-8")
            with self.assertRaises(SourceProfileError) as ctx:
                profile_source_csv(path)
        self.assertEqual(ctx.exception.code, "empty_source")

    def test_16_url_rejected(self):
        with self.assertRaises(SourceProfileError) as ctx:
            profile_source_csv(Path("https://example.invalid/source.csv"))
        self.assertEqual(ctx.exception.code, "remote_source_not_allowed")

    def test_17_project_escape_rejected(self):
        with self.assertRaises(SourceProfileError) as ctx:
            profile_source_csv(Path("..") / "outside.csv")
        self.assertEqual(ctx.exception.code, "path_escape_not_allowed")

    def test_18_exact_alias_hit(self):
        profiles, _ = profile_source_csv(GENERIC_SOURCE)
        field = next(profile for profile in profiles if profile.name == "contact_email")
        suggestion = score_source_field(field, build_target_field_index(_contract()), FakeEmbeddingBackend())
        self.assertTrue(suggestion.top_candidates[0].alias_hit)

    def test_19_alias_confidence_floor(self):
        profiles, _ = profile_source_csv(GENERIC_SOURCE)
        field = next(profile for profile in profiles if profile.name == "client_name")
        suggestion = score_source_field(field, build_target_field_index(_contract()), FakeEmbeddingBackend())
        self.assertGreaterEqual(suggestion.confidence, HIGH_CONFIDENCE)
        self.assertEqual(suggestion.mapping_basis, "alias")

    def test_20_semantic_only_candidate(self):
        report = _generic_report()
        mapping = next(item for item in report["mappings"] if item["source_field"] == "telephone_number")
        self.assertEqual(mapping["recommendation"], "customer.phone")
        self.assertNotEqual(mapping["mapping_basis"], "alias")

    def test_21_fuzzy_contribution(self):
        profiles, _ = profile_source_csv(SUPPLIER_SOURCE)
        field = next(profile for profile in profiles if profile.name == "legal_entity_code")
        suggestion = score_source_field(field, build_target_field_index(_contract(SUPPLIER_CONTRACT, SUPPLIER_DATA)), FakeEmbeddingBackend())
        self.assertEqual(suggestion.top_candidates[0].target, "supplier_company.company_code")
        self.assertGreater(suggestion.top_candidates[0].fuzzy_score, 0.3)

    def test_22_type_gate_cannot_increase_score(self):
        profiles, _ = profile_source_csv(GENERIC_SOURCE)
        field = next(profile for profile in profiles if profile.name == "marketing_opt_in")
        target = build_target_field_index(_contract())[0]
        gate, _ = _type_gate(field, target)
        self.assertLessEqual(gate, 1.0)

    def test_23_no_lexical_anchor_false_friend_rule(self):
        report = _generic_report()
        mapping = next(item for item in report["mappings"] if item["source_field"] == "marketing_opt_in")
        self.assertEqual(mapping["status"], "no_confident_target")

    def test_24_deterministic_candidate_ordering(self):
        first = _generic_report()
        second = _generic_report()
        self.assertEqual(first["mappings"], second["mappings"])

    def test_25_unknown_semantic_type_safe_behavior(self):
        descriptor = yaml.safe_load(GENERIC_CONTRACT.read_text(encoding="utf-8"))
        descriptor["resources"][0]["schema"]["fields"][0]["carveops"]["semantic_type"] = "future_type"
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            root = Path(temp_dir)
            data = root / "data"
            data.mkdir()
            for csv_file in GENERIC_DATA.glob("*.csv"):
                (data / csv_file.name).write_bytes(csv_file.read_bytes())
            contract_path = root / "datapackage.yaml"
            contract_path.write_text(yaml.safe_dump(descriptor), encoding="utf-8")
            report = suggest_contract_mappings(
                load_migration_contract(contract_path, data),
                GENERIC_SOURCE,
                embedding_backend=FakeEmbeddingBackend(),
            )
        self.assertEqual(len(report["mappings"]), 12)

    def test_26_generic_uses_common_engine(self):
        report = _generic_report()
        self.assertEqual(report["_meta"]["component"], "contract_field_mapping")

    def test_27_supplier_uses_common_engine(self):
        report = suggest_contract_mappings(
            _contract(SUPPLIER_CONTRACT, SUPPLIER_DATA),
            SUPPLIER_SOURCE,
            embedding_backend=FakeEmbeddingBackend(),
        )
        self.assertEqual(report["_meta"]["component"], "contract_field_mapping")

    def test_28_no_adapter_branch(self):
        text = "\n".join(path.read_text(encoding="utf-8") for path in (PROJECT_ROOT / "src/core/mapping").glob("*.py"))
        self.assertNotRegex(text, r"\bif\s+.*adapter\b|\belif\s+.*adapter\b|adapter\s*==")

    def test_29_no_ground_truth_import_or_read(self):
        files = [
            PROJECT_ROOT / "src/core/mapping/profiler.py",
            PROJECT_ROOT / "src/core/mapping/target_index.py",
            PROJECT_ROOT / "src/core/mapping/scorer.py",
            PROJECT_ROOT / "src/core/mapping/engine.py",
            PROJECT_ROOT / "src/core/mapping/runtime.py",
            PROJECT_ROOT / "src/tools/suggest_contract_mappings.py",
        ]
        forbidden_patterns = ("ground_truth.json", "ground_truth_path", "ground-truth", "Ground Truth")
        self.assertFalse(
            any(
                pattern in path.read_text(encoding="utf-8")
                for path in files
                for pattern in forbidden_patterns
            )
        )

    def test_30_mapping_source_order_stable(self):
        report = _generic_report()
        self.assertEqual([item["source_field"] for item in report["mappings"]][0], "legacy_client_id")
        self.assertEqual([item["source_field"] for item in report["mappings"]][-1], "marketing_opt_in")

    def test_31_report_contains_relative_paths(self):
        text = json.dumps(_generic_report(), ensure_ascii=False)
        self.assertNotRegex(text, r"[A-Za-z]:[\\/]")

    def test_32_content_sha_stable(self):
        first = _generic_report()
        second = _generic_report()
        self.assertEqual(first["_run_info"]["content_sha256"], second["_run_info"]["content_sha256"])

    def test_33_source_change_changes_sha(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            source = Path(temp_dir) / "source.csv"
            source.write_bytes(GENERIC_SOURCE.read_bytes())
            report_a = suggest_contract_mappings(_contract(), source, embedding_backend=FakeEmbeddingBackend())
            rows = list(csv.reader(source.read_text(encoding="utf-8").splitlines()))
            rows[1][1] = "Synthetic Changed Customer"
            source.write_text("\n".join(",".join(row) for row in rows) + "\n", encoding="utf-8")
            report_b = suggest_contract_mappings(_contract(), source, embedding_backend=FakeEmbeddingBackend())
        self.assertNotEqual(report_a["_run_info"]["content_sha256"], report_b["_run_info"]["content_sha256"])

    def test_34_contract_change_changes_sha(self):
        report_a = _generic_report()
        descriptor = yaml.safe_load(GENERIC_CONTRACT.read_text(encoding="utf-8"))
        descriptor["resources"][0]["schema"]["fields"][0]["carveops"]["description"] += " Changed."
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            root = Path(temp_dir)
            data = root / "data"
            data.mkdir()
            for csv_file in GENERIC_DATA.glob("*.csv"):
                (data / csv_file.name).write_bytes(csv_file.read_bytes())
            contract_path = root / "datapackage.yaml"
            contract_path.write_text(yaml.safe_dump(descriptor), encoding="utf-8")
            report_b = suggest_contract_mappings(load_migration_contract(contract_path, data), GENERIC_SOURCE, embedding_backend=FakeEmbeddingBackend())
        self.assertNotEqual(report_a["_run_info"]["content_sha256"], report_b["_run_info"]["content_sha256"])

    def test_35_input_csv_unchanged(self):
        before = _sha(GENERIC_SOURCE)
        _generic_report()
        self.assertEqual(before, _sha(GENERIC_SOURCE))

    def test_35b_runtime_scorer_inventory_is_explicit(self):
        self.assertEqual(SUPPORTED_RUNTIME_SCORERS, {"baseline", "precision_tiered_v4"})

    def test_35c_cli_parser_defaults_to_baseline_and_accepts_v4(self):
        parser = build_parser()
        scorer_action = next(action for action in parser._actions if action.dest == "scorer")
        self.assertEqual(scorer_action.default, BASELINE_SCORER_ID)
        self.assertEqual(set(scorer_action.choices), SUPPORTED_RUNTIME_SCORERS)

        parsed = parser.parse_args([
            "--contract", str(GENERIC_CONTRACT),
            "--data-root", str(GENERIC_DATA),
            "--source", str(GENERIC_SOURCE),
            "--output", str(Path(tempfile.gettempdir()) / "runtime-v4.json"),
            "--scorer", PRECISION_TIERED_V4_SCORER_ID,
        ])
        self.assertEqual(parsed.scorer, PRECISION_TIERED_V4_SCORER_ID)

        defaulted = parser.parse_args([
            "--contract", str(GENERIC_CONTRACT),
            "--data-root", str(GENERIC_DATA),
            "--source", str(GENERIC_SOURCE),
            "--output", str(Path(tempfile.gettempdir()) / "runtime-baseline.json"),
        ])
        self.assertEqual(defaulted.scorer, BASELINE_SCORER_ID)

        with self.assertRaises(SystemExit):
            parser.parse_args([
                "--contract", str(GENERIC_CONTRACT),
                "--data-root", str(GENERIC_DATA),
                "--source", str(GENERIC_SOURCE),
                "--output", str(Path(tempfile.gettempdir()) / "runtime-future.json"),
                "--scorer", "future",
            ])

    def test_35d_unknown_runtime_scorer_rejected(self):
        with self.assertRaises(RuntimeScorerError) as ctx:
            suggest_runtime_contract_mappings(
                _contract(),
                GENERIC_SOURCE,
                scorer_id="future",
                embedding_backend=FakeEmbeddingBackend(),
            )
        self.assertEqual(ctx.exception.code, "unknown_runtime_scorer")

    def test_35e_runtime_baseline_dispatch_matches_engine(self):
        previous = os.environ.get("CARVEOPS_OMIT_TIMESTAMP")
        os.environ["CARVEOPS_OMIT_TIMESTAMP"] = "1"
        try:
            direct = suggest_contract_mappings(
                _contract(),
                GENERIC_SOURCE,
                embedding_backend=FakeEmbeddingBackend(),
            )
            dispatched = suggest_runtime_contract_mappings(
                _contract(),
                GENERIC_SOURCE,
                scorer_id=BASELINE_SCORER_ID,
                embedding_backend=FakeEmbeddingBackend(),
            )
        finally:
            if previous is None:
                os.environ.pop("CARVEOPS_OMIT_TIMESTAMP", None)
            else:
                os.environ["CARVEOPS_OMIT_TIMESTAMP"] = previous
        self.assertEqual(dispatched, direct)

    def test_35f_runtime_v4_dispatch_preserves_direct_v4_mappings(self):
        direct = suggest_contract_mappings_v4(
            _contract(),
            GENERIC_SOURCE,
            embedding_backend=FakeEmbeddingBackend(),
        )
        dispatched = suggest_runtime_contract_mappings(
            _contract(),
            GENERIC_SOURCE,
            scorer_id=PRECISION_TIERED_V4_SCORER_ID,
            embedding_backend=FakeEmbeddingBackend(),
        )
        self.assertEqual(dispatched["mappings"], direct["mappings"])
        self.assertIn("summary", dispatched)
        self.assertIn("unmapped_target_fields", dispatched)
        self.assertIn("_run_info", dispatched)
        self.assertEqual(dispatched["_meta"]["scorer_variant"], PRECISION_TIERED_V4_SCORER_ID)
        self.assertEqual(dispatched["_meta"]["scorer_id"], PRECISION_TIERED_V4_SCORER_ID)
        self.assertEqual(dispatched["_meta"]["feature_version"], "precision_tiered_interaction_v1")
        self.assertFalse(dispatched["_meta"]["ground_truth_used"])
        self.assertFalse(dispatched["_meta"]["ground_truth_used_for_candidate_generation"])
        self.assertTrue(dispatched["_meta"]["experimental"])
        self.assertFalse(dispatched["_meta"]["production_scorer_modified"])
        for field in (
            "suggested",
            "needs_review",
            "possible_false_friend",
            "no_confident_target",
            "alias_based",
            "semantic_based",
            "target_coverage",
        ):
            self.assertIn(field, dispatched["summary"])
        body = {key: value for key, value in dispatched.items() if key != "_run_info"}
        from src.core.hashing import canonical_json_content_sha256

        self.assertEqual(dispatched["_run_info"]["content_sha256"], canonical_json_content_sha256(body))
        text = json.dumps(dispatched, ensure_ascii=False)
        self.assertNotIn("ground_truth.json", text)
        self.assertNotIn("answer_source_path", text)
        first_candidate = dispatched["mappings"][0]["top_candidates"][0]
        for key in (
            "activated_interactions",
            "interaction_evidence",
            "diagnostic_bonus",
            "supportive_bonus",
            "top1_selection_reason",
        ):
            self.assertIn(key, first_candidate)

    def test_36_top1_metric(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            output = Path(temp_dir) / "mapping.json"
            write_mapping_report(_generic_report(), output)
            evaluation = evaluate_mapping_report(output, GENERIC_TRUTH)
        self.assertEqual(evaluation["summary"]["mapped_ground_truth_fields"], 11)
        self.assertGreater(evaluation["summary"]["top1_accuracy"], 0.8)

    def test_37_top3_metric(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            output = Path(temp_dir) / "mapping.json"
            write_mapping_report(_generic_report(), output)
            evaluation = evaluate_mapping_report(output, GENERIC_TRUTH)
        self.assertEqual(evaluation["summary"]["top3_recall"], 1.0)

    def test_38_high_confidence_precision(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            output = Path(temp_dir) / "mapping.json"
            write_mapping_report(_generic_report(), output)
            evaluation = evaluate_mapping_report(output, GENERIC_TRUTH)
        self.assertEqual(evaluation["summary"]["high_confidence_precision"], 1.0)

    def test_39_no_target_accuracy(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            output = Path(temp_dir) / "mapping.json"
            write_mapping_report(_generic_report(), output)
            evaluation = evaluate_mapping_report(output, GENERIC_TRUTH)
        self.assertEqual(evaluation["summary"]["no_target_accuracy"], 1.0)

    def test_40_group_breakdown(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            output = Path(temp_dir) / "mapping.json"
            write_mapping_report(_generic_report(), output)
            evaluation = evaluate_mapping_report(output, GENERIC_TRUTH)
        self.assertEqual(evaluation["by_evaluation_group"]["alias_backed"]["fields"], 6)
        self.assertEqual(evaluation["by_evaluation_group"]["semantic_only"]["fields"], 5)
        self.assertEqual(evaluation["by_evaluation_group"]["no_target"]["fields"], 1)

    def test_41_evaluator_is_only_ground_truth_consumer(self):
        allowed = {
            str(PROJECT_ROOT / "src/core/mapping/evaluator.py"),
            str(PROJECT_ROOT / "src/core/mapping/protocol_lock.py"),
            str(PROJECT_ROOT / "src/tools/evaluate_contract_mappings.py"),
            str(PROJECT_ROOT / "tests/test_contract_mapping.py"),
            str(PROJECT_ROOT / "scripts/smoke_test_contract_mapping.py"),
        }
        files = list((PROJECT_ROOT / "src/core/mapping").glob("*.py"))
        files.extend([
            PROJECT_ROOT / "src/tools/suggest_contract_mappings.py",
            PROJECT_ROOT / "src/tools/evaluate_contract_mappings.py",
            PROJECT_ROOT / "tests/test_contract_mapping.py",
            PROJECT_ROOT / "scripts/smoke_test_contract_mapping.py",
        ])
        offenders = [
            str(path)
            for path in files
            if any(pattern in path.read_text(encoding="utf-8") for pattern in (
                "ground_truth.json",
                "ground_truth_path",
                "ground-truth",
                "Ground Truth",
            ))
            and str(path) not in allowed
        ]
        self.assertEqual(offenders, [])

    def test_42_generic_cli_returns_zero(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            output = Path(temp_dir) / "generic.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "src/tools/suggest_contract_mappings.py",
                    "--contract", str(GENERIC_CONTRACT),
                    "--data-root", str(GENERIC_DATA),
                    "--source", str(GENERIC_SOURCE),
                    "--output", str(output),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_43_supplier_cli_returns_zero(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            output = Path(temp_dir) / "supplier.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "src/tools/suggest_contract_mappings.py",
                    "--contract", str(SUPPLIER_CONTRACT),
                    "--data-root", str(SUPPLIER_DATA),
                    "--source", str(SUPPLIER_SOURCE),
                    "--output", str(output),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_44_invalid_input_returns_two(self):
        result = subprocess.run(
            [
                sys.executable,
                "src/tools/suggest_contract_mappings.py",
                "--contract", str(GENERIC_CONTRACT),
                "--data-root", str(GENERIC_DATA),
                "--source", "https://example.invalid/source.csv",
                "--output", "data/synthetic/tmp_mapping.json",
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)

    def test_45_smoke_passes(self):
        result = subprocess.run(
            [sys.executable, "scripts/smoke_test_contract_mapping.py"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Validation: valid", result.stdout)


if __name__ == "__main__":
    unittest.main()
