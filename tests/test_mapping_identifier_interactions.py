from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.mapping.identifier_interactions import (
    FEATURE_VERSION,
    IDENTIFIER_BONUS_WEIGHT,
    MAX_IDENTIFIER_BONUS,
    identifier_interaction_evidence,
    source_identifier_evidence,
    target_identifier_evidence,
    tokenize_identifier,
)
from src.core.mapping.models import ContractTargetField, SourceFieldProfile
from src.core.mapping.runtime import (
    BASELINE_SCORER_ID,
    EXPERIMENTAL_RUNTIME_SCORERS,
    SUPPORTED_RUNTIME_SCORERS,
    suggest_runtime_contract_mappings,
)
from src.core.mapping.scorer_v4 import SCORER_ID as PRECISION_TIERED_V4_SCORER_ID
from src.core.mapping.scorer_v5 import SCORER_ID as PRECISION_TIERED_V5_SCORER_ID
from src.core.mapping.scorer_v5 import metadata, score_all_candidates_v5, score_source_field_v5


def _profile(name: str) -> SourceFieldProfile:
    return SourceFieldProfile(
        name=name,
        inferred_kind="string",
        row_count=3,
        present_count=3,
        missing_count=0,
        missing_ratio=0.0,
        distinct_count=3,
        distinct_ratio=1.0,
        observed_max_length=16,
        samples=("A1", "A2", "A3"),
    )


def _target(
    name: str,
    *,
    resource: str = "resource",
    aliases: tuple[str, ...] = (),
    semantic_type: str = "identifier",
    qualified_name: str | None = None,
) -> ContractTargetField:
    return ContractTargetField(
        resource=resource,
        name=name,
        qualified_name=qualified_name or f"{resource}.{name}",
        frictionless_type="string",
        required=False,
        unique=False,
        primary_key=False,
        max_length=None,
        enum_values=(),
        pattern=None,
        description="",
        aliases=aliases,
        semantic_type=semantic_type,
    )


def _candidate(target: str, rank: int, score: float) -> dict:
    return {
        "target": target,
        "rank": rank,
        "score": score,
        "semantic_score": score,
        "fuzzy_score": score,
        "alias_hit": False,
        "alias_source": None,
        "lexical_overlap": 0.0,
        "type_gate": 1.0,
        "warnings": [],
        "activated_interactions": [],
        "interaction_evidence": [],
        "diagnostic_bonus": 0.0,
        "supportive_bonus": 0.0,
        "interaction_adjusted_score": score,
        "top1_eligible": False,
        "top1_selection_reason": "v3_top1_locked_no_diagnostic_challenger" if rank == 1 else "not_selected_for_top1",
    }


class IdentifierInteractionTests(unittest.TestCase):
    def test_01_tokenizer_handles_common_identifier_formats(self) -> None:
        cases = {
            "client_num": ("client", "num"),
            "vendor-identifier": ("vendor", "identifier"),
            "item.priceID": ("item", "price", "id"),
            "mobile phone": ("mobile", "phone"),
            "customerID": ("customer", "id"),
            "GL100Code": ("gl", "100", "code"),
        }
        for value, expected in cases.items():
            self.assertEqual(tokenize_identifier(value), expected)

    def test_02_entity_synonyms_are_symmetric_concepts(self) -> None:
        self.assertIn("customer_entity", source_identifier_evidence(_profile("client_num"))["source_concepts"])
        self.assertIn("supplier_entity", source_identifier_evidence(_profile("vendor_identifier"))["source_concepts"])
        self.assertIn("item_entity", source_identifier_evidence(_profile("product_key"))["source_concepts"])

    def test_03_identifier_tokens_are_recognized(self) -> None:
        for value in ["id", "identifier", "number", "no", "num", "nr", "code", "sku", "key", "ref", "reference"]:
            self.assertIn("generic_identifier", source_identifier_evidence(_profile(f"entity_{value}"))["source_concepts"])

    def test_04_interaction_activates_when_all_conditions_hold(self) -> None:
        evidence = identifier_interaction_evidence(
            _profile("client_num"),
            _target("customer_id", aliases=("customer_number",), semantic_type="identifier"),
            v4_score=0.6,
        )
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["interaction_id"], "shared_entity_identifier")
        self.assertEqual(evidence[0]["matched_entity_concepts"], ["customer_entity"])
        self.assertEqual(evidence[0]["tier"], "diagnostic")

    def test_05_missing_entity_does_not_activate(self) -> None:
        evidence = identifier_interaction_evidence(
            _profile("row_number"),
            _target("customer_id", semantic_type="identifier"),
            v4_score=0.6,
        )
        self.assertEqual(evidence, [])

    def test_06_missing_identifier_does_not_activate(self) -> None:
        evidence = identifier_interaction_evidence(
            _profile("client_phone"),
            _target("customer_phone", semantic_type="phone"),
            v4_score=0.6,
        )
        self.assertEqual(evidence, [])

    def test_07_target_resource_is_not_identifier_evidence(self) -> None:
        target = _target("phone", resource="customer", semantic_type="phone")
        self.assertNotIn("customer_entity", target_identifier_evidence(target)["target_concepts"])
        evidence = identifier_interaction_evidence(_profile("client_num"), target, v4_score=0.6)
        self.assertEqual(evidence, [])

    def test_08_no_target_style_field_does_not_activate(self) -> None:
        evidence = identifier_interaction_evidence(
            _profile("migration_status"),
            _target("customer_id", semantic_type="identifier"),
            v4_score=0.6,
        )
        self.assertEqual(evidence, [])

    def test_09_multiple_entities_do_not_cross_match(self) -> None:
        evidence = identifier_interaction_evidence(
            _profile("vendor_identifier"),
            _target("customer_id", semantic_type="identifier"),
            v4_score=0.6,
        )
        self.assertEqual(evidence, [])

    def test_10_evidence_is_deterministic(self) -> None:
        first = identifier_interaction_evidence(_profile("product_key"), _target("item_code"), v4_score=0.55)
        second = identifier_interaction_evidence(_profile("product_key"), _target("item_code"), v4_score=0.55)
        self.assertEqual(first, second)

    def test_11_input_candidates_are_not_mutated(self) -> None:
        profile = _profile("client_num")
        targets = [
            _target("phone", semantic_type="phone", qualified_name="customer.phone"),
            _target("customer_id", qualified_name="customer.customer_id"),
        ]
        candidates = [
            _candidate("customer.phone", 1, 0.62),
            _candidate("customer.customer_id", 2, 0.61),
        ]
        before = copy.deepcopy(candidates)
        score_all_candidates_v5(profile, candidates, targets)
        self.assertEqual(candidates, before)

    def test_12_without_interaction_core_v5_matches_v4(self) -> None:
        profile = _profile("phone_text")
        targets = [
            _target("phone", semantic_type="phone", qualified_name="customer.phone"),
            _target("tax_number", semantic_type="tax_identifier", qualified_name="customer.tax_number"),
        ]
        candidates = [
            _candidate("customer.phone", 1, 0.71),
            _candidate("customer.tax_number", 2, 0.52),
        ]
        result = score_source_field_v5(profile, candidates, targets)
        self.assertEqual(result["recommendation"], "customer.phone")
        self.assertEqual(result["status"], "suggested")
        self.assertEqual([(item["target"], item["score"], item["rank"]) for item in result["top_candidates"]], [
            ("customer.phone", 0.71, 1),
            ("customer.tax_number", 0.52, 2),
        ])

    def test_13_interaction_bonus_has_upper_bound(self) -> None:
        evidence = identifier_interaction_evidence(_profile("client_num"), _target("customer_id"), v4_score=0.1)
        self.assertLessEqual(evidence[0]["bonus"], MAX_IDENTIFIER_BONUS)
        self.assertEqual(evidence[0]["bonus_weight"], IDENTIFIER_BONUS_WEIGHT)

    def test_14_challenger_must_strictly_exceed_top1(self) -> None:
        profile = _profile("client_num")
        targets = [
            _target("phone", semantic_type="phone", qualified_name="customer.phone"),
            _target("customer_id", qualified_name="customer.customer_id"),
        ]
        candidates = [
            _candidate("customer.phone", 1, 0.7),
            _candidate("customer.customer_id", 2, 0.5588),
        ]
        result = score_all_candidates_v5(profile, candidates, targets)
        self.assertEqual(result[0]["target"], "customer.phone")
        self.assertEqual(result[0]["v5_top1_selection_reason"], "v4_top1_retained_no_identifier_challenger")

    def test_15_tie_retains_v4_top1(self) -> None:
        profile = _profile("vendor_identifier")
        targets = [
            _target("organization_name", semantic_type="organization_name", qualified_name="supplier.organization_name"),
            _target("supplier_id", qualified_name="supplier.supplier_id"),
        ]
        candidates = [
            _candidate("supplier.organization_name", 1, 0.7),
            _candidate("supplier.supplier_id", 2, 0.5588),
        ]
        result = score_all_candidates_v5(profile, candidates, targets)
        self.assertEqual(result[0]["target"], "supplier.organization_name")

    def test_16_correctly_reranks_top3_when_challenger_exceeds(self) -> None:
        profile = _profile("product_key")
        targets = [
            _target("item_name", semantic_type="product_name", qualified_name="item.item_name"),
            _target("item_code", qualified_name="item.item_code", semantic_type="product_identifier"),
            _target("uom", semantic_type="unit_of_measure", qualified_name="item_price.uom"),
        ]
        candidates = [
            _candidate("item.item_name", 1, 0.62),
            _candidate("item.item_code", 2, 0.61),
            _candidate("item_price.uom", 3, 0.2),
        ]
        result = score_all_candidates_v5(profile, candidates, targets)
        self.assertEqual([item["target"] for item in result[:3]], ["item.item_code", "item.item_name", "item_price.uom"])
        self.assertEqual(result[0]["v5_top1_selection_reason"], "identifier_adjusted_score_strictly_exceeded_v4_top1")

    def test_17_metadata_and_ground_truth_flags(self) -> None:
        data = metadata()
        self.assertTrue(data["experimental"])
        self.assertEqual(data["scorer_id"], "precision_tiered_v5")
        self.assertEqual(data["parent_scorer"], "precision_tiered_v4")
        self.assertEqual(data["feature_version"], FEATURE_VERSION)
        for key, value in data.items():
            if key.startswith("ground_truth_used"):
                self.assertFalse(value)
        self.assertFalse(data["production_scorer_modified"])

    def test_18_runtime_baseline_default_unchanged(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            source = Path(temp_dir) / "source.csv"
            source.write_text("field\nvalue\n", encoding="utf-8")
            with patch("src.core.mapping.runtime.suggest_contract_mappings", return_value=_runtime_report()) as baseline:
                suggest_runtime_contract_mappings(object(), source)
        baseline.assert_called_once()
        self.assertEqual(BASELINE_SCORER_ID, "baseline")

    def test_19_runtime_v4_dispatch_unchanged(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            source = Path(temp_dir) / "source.csv"
            source.write_text("field\nvalue\n", encoding="utf-8")
            with patch("src.core.mapping.runtime.suggest_contract_mappings_v4", return_value=_runtime_report()) as v4:
                suggest_runtime_contract_mappings(_runtime_contract(), source, scorer_id=PRECISION_TIERED_V4_SCORER_ID)
        v4.assert_called_once()

    def test_20_runtime_v5_opt_in_dispatch(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            source = Path(temp_dir) / "source.csv"
            source.write_text("field\nvalue\n", encoding="utf-8")
            with patch("src.core.mapping.runtime.suggest_contract_mappings_v5", return_value=_runtime_report()) as v5:
                suggest_runtime_contract_mappings(_runtime_contract(), source, scorer_id=PRECISION_TIERED_V5_SCORER_ID)
        v5.assert_called_once()
        self.assertNotIn(PRECISION_TIERED_V5_SCORER_ID, SUPPORTED_RUNTIME_SCORERS)
        self.assertIn(PRECISION_TIERED_V5_SCORER_ID, EXPERIMENTAL_RUNTIME_SCORERS)

    def test_21_import_scorer_v5_does_not_construct_sentence_transformer(self) -> None:
        source = (PROJECT_ROOT / "src" / "core" / "mapping" / "scorer_v5.py").read_text(encoding="utf-8")
        self.assertNotIn("SentenceTransformer(", source)

    def test_22_scorer_v5_does_not_import_diagnostic_fixture_or_evaluator(self) -> None:
        source = (PROJECT_ROOT / "src" / "core" / "mapping" / "scorer_v5.py").read_text(encoding="utf-8")
        self.assertNotIn("schema_matching_identifier_robustness", source)
        self.assertNotIn("evaluate_schema_matching_identifier_robustness", source)


def _runtime_contract():
    class Contract:
        contract_id = "unit"
        version = "1"
        descriptor = {"resources": []}

    return Contract()


def _runtime_report() -> dict:
    return {
        "_meta": {
            "component": "contract_field_mapping",
            "contract_id": "unit",
            "contract_version": "1",
        },
        "mappings": [],
    }


if __name__ == "__main__":
    unittest.main()
