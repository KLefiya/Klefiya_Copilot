from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.mapping.models import ContractTargetField, SourceFieldProfile
from src.core.mapping.scorer_v4 import score_all_candidates_v4, score_source_field_v4
from src.core.mapping.sparse_interactions import (
    CANONICAL_EVIDENCE_SOURCE_SHA256,
    active_interactions,
    concept_evidence_from_texts,
    metadata,
    source_concept_evidence,
    target_concept_evidence,
    tokenize,
)


def _profile(name: str = "source_field") -> SourceFieldProfile:
    return SourceFieldProfile(
        name=name,
        inferred_kind="string",
        row_count=2,
        present_count=2,
        missing_count=0,
        missing_ratio=0.0,
        distinct_count=2,
        distinct_ratio=1.0,
        observed_max_length=12,
        samples=("A", "B"),
    )


def _target(name: str, aliases: tuple[str, ...] = ()) -> ContractTargetField:
    return ContractTargetField(
        resource="resource",
        name=name,
        qualified_name=f"resource.{name}",
        frictionless_type="string",
        required=False,
        unique=False,
        primary_key=False,
        max_length=None,
        enum_values=(),
        pattern=None,
        description="not used by sparse interactions",
        aliases=aliases,
        semantic_type="not_used",
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
    }


class SparseInteractionEvidenceTests(unittest.TestCase):
    def test_tokenizer_matches_canonical_a_behavior(self) -> None:
        self.assertEqual(tokenize("bankCustomerRef-1.key"), ("1", "bank", "customer", "key", "ref"))

    def test_atomic_concepts_and_no_balanced_v2_concepts(self) -> None:
        evidence = concept_evidence_from_texts(("bank_customer_reference",))
        self.assertEqual(set(evidence["matched_concepts"]), {"institution", "generic_identifier"})
        self.assertNotIn("customer_party", metadata()["concept_tokens"])
        self.assertFalse(metadata()["global_weighted_jaccard"])
        self.assertEqual(CANONICAL_EVIDENCE_SOURCE_SHA256, "68d2d4f35b8f59000788fd44f86a442dd7ef9df8676ac40575314b10c2062c22")

    def test_source_and_target_construction_ignore_descriptions_and_values(self) -> None:
        source = source_concept_evidence(_profile("clearing_code"))
        target = target_concept_evidence(_target("routing_number", ("ignored_alias",)))
        self.assertIn("routing_identifier", source["matched_concepts"])
        self.assertIn("routing_identifier", target["matched_concepts"])
        self.assertNotIn("not", target["tokens"])

    def test_interaction_activation_positive_negative_and_ordering(self) -> None:
        both = active_interactions(
            {"routing_identifier", "institution", "generic_identifier"},
            {"routing_identifier", "institution", "generic_identifier"},
            v3_score=0.25,
        )
        self.assertEqual([item["interaction_id"] for item in both], ["institutional_key_support", "routing_to_routing"])
        self.assertEqual([item["bonus"] for item in both], [0.075, 0.075])
        self.assertEqual(active_interactions({"institution"}, {"institution", "generic_identifier"}, v3_score=0.5), [])
        self.assertFalse(any("ground_truth" in key for row in both for key in row))


class SparseInteractionScoringRankingTests(unittest.TestCase):
    def test_no_interaction_exact_v3_score_parity(self) -> None:
        targets = [_target("plain_name")]
        ranked = score_all_candidates_v4(_profile("plain_source"), [_candidate("resource.plain_name", 1, 0.51)], targets)
        self.assertEqual(ranked[0]["score"], 0.51)
        self.assertEqual(ranked[0]["interaction_adjusted_score"], 0.51)
        self.assertEqual(ranked[0]["activated_interactions"], [])

    def test_diagnostic_bonus_and_low_score_recommendation_boundary(self) -> None:
        targets = [_target("country_code"), _target("routing_number")]
        candidates = [_candidate("resource.country_code", 1, 0.31), _candidate("resource.routing_number", 2, 0.27)]
        suggestion = score_source_field_v4(_profile("clearing_code"), candidates, targets)
        self.assertEqual(suggestion["top_candidates"][0]["target"], "resource.routing_number")
        self.assertAlmostEqual(suggestion["top_candidates"][0]["diagnostic_bonus"], 0.073)
        self.assertIsNone(suggestion["recommendation"])
        self.assertEqual(suggestion["status"], "no_confident_target")

    def test_diagnostic_requires_strictly_greater_adjusted_score(self) -> None:
        targets = [_target("country_code"), _target("routing_number")]
        candidates = [_candidate("resource.country_code", 1, 0.91), _candidate("resource.routing_number", 2, 0.8889)]
        ranked = score_all_candidates_v4(_profile("clearing_code"), candidates, targets)
        self.assertEqual(ranked[0]["target"], "resource.country_code")

    def test_supportive_cannot_displace_top1_but_can_improve_rank2(self) -> None:
        targets = [_target("customer_id", ("customer_reference",)), _target("bank_id", ("bank_identifier",)), _target("iban")]
        candidates = [
            _candidate("resource.customer_id", 1, 0.6526),
            _candidate("resource.iban", 2, 0.64),
            _candidate("resource.bank_id", 3, 0.6262),
        ]
        ranked = score_all_candidates_v4(_profile("bank_customer_reference"), candidates, targets)
        self.assertEqual([item["target"] for item in ranked[:3]], ["resource.customer_id", "resource.bank_id", "resource.iban"])
        self.assertEqual(ranked[1]["activated_interactions"], ["institutional_key_support"])

    def test_two_bonuses_are_additive_and_clamped(self) -> None:
        targets = [_target("routing_bank_key")]
        ranked = score_all_candidates_v4(_profile("routing_bank_ref"), [_candidate("resource.routing_bank_key", 1, 0.98)], targets)
        self.assertEqual(ranked[0]["activated_interactions"], ["institutional_key_support", "routing_to_routing"])
        self.assertEqual(ranked[0]["score"], 0.984)

    def test_deterministic_tie_break_and_input_order_invariance(self) -> None:
        targets = [_target("bank_key_a"), _target("bank_key_b"), _target("customer_id")]
        candidates = [
            _candidate("resource.customer_id", 1, 0.91),
            _candidate("resource.bank_key_b", 2, 0.5),
            _candidate("resource.bank_key_a", 3, 0.5),
        ]
        forward = score_all_candidates_v4(_profile("bank_ref"), candidates, targets)
        reverse = score_all_candidates_v4(_profile("bank_ref"), list(reversed(candidates)), targets)
        self.assertEqual([item["target"] for item in forward], [item["target"] for item in reverse])
        self.assertEqual([item["target"] for item in forward[:3]], ["resource.customer_id", "resource.bank_key_a", "resource.bank_key_b"])

    def test_no_interaction_recommendation_status_v3_parity(self) -> None:
        targets = [_target("plain_name")]
        suggestion = score_source_field_v4(_profile("plain_source"), [_candidate("resource.plain_name", 1, 0.8)], targets)
        self.assertEqual(suggestion["recommendation"], "resource.plain_name")
        self.assertEqual(suggestion["status"], "suggested")


if __name__ == "__main__":
    unittest.main()
