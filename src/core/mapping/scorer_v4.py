from __future__ import annotations

from typing import Any

from src.core.mapping.models import ContractTargetField, MappingCandidate, SourceFieldProfile
from src.core.mapping.resource_context import resource_context_for_index
from src.core.mapping.scorer import (
    DEFAULT_MODEL_NAME,
    TOP_N_CANDIDATES,
    EmbeddingBackend,
    _basis,
    _status,
    load_embedding_backend,
)
from src.core.mapping.scorer_v2 import score_all_candidates_v2
from src.core.mapping.scorer_v3 import SCORER_ID as TARGET_CONTEXT_SCORER_ID
from src.core.mapping.scorer_v3 import score_all_candidates_v3
from src.core.mapping.sparse_interactions import (
    FEATURE_VERSION,
    active_interactions,
    metadata as interaction_metadata,
    source_concept_evidence,
    target_concept_evidence,
)
from src.tools.data_profile import attach_run_info


SCORER_ID = "precision_tiered_v4"
PARENT_SCORER_ID = TARGET_CONTEXT_SCORER_ID


def score_source_fields_v4(
    profiles: list[SourceFieldProfile],
    targets: list[ContractTargetField],
    backend: EmbeddingBackend,
) -> list[dict[str, Any]]:
    source_fields = [profile.name for profile in profiles]
    all_v2_candidates = {
        profile.name: score_all_candidates_v2(profile, targets, backend)
        for profile in profiles
    }
    all_v3_candidates = {
        profile.name: score_all_candidates_v3(
            profile,
            all_v2_candidates[profile.name],
            resource_context_for_index(source_fields, all_v2_candidates, index),
        )
        for index, profile in enumerate(profiles)
    }
    return [
        score_source_field_v4(profile, all_v3_candidates[profile.name], targets)
        for profile in profiles
    ]


def score_source_field_v4(
    profile: SourceFieldProfile,
    v3_candidates: list[dict[str, Any]],
    targets: list[ContractTargetField],
) -> dict[str, Any]:
    ranked = score_all_candidates_v4(profile, v3_candidates, targets)
    top = ranked[:TOP_N_CANDIDATES]
    best = _candidate_from_dict(top[0])
    basis = _basis(best)
    status, band, reasons = _status(best, basis)
    recommendation = best.target if status != "no_confident_target" else None
    confidence = best.score if recommendation else 0.0
    return {
        "source_field": profile.name,
        "status": status,
        "recommendation": recommendation,
        "confidence": confidence,
        "band": band,
        "mapping_basis": basis,
        "source_profile": profile.to_dict(),
        "review_reasons": list(reasons),
        "top_candidates": top,
    }


def score_all_candidates_v4(
    profile: SourceFieldProfile,
    v3_candidates: list[dict[str, Any]],
    targets: list[ContractTargetField],
) -> list[dict[str, Any]]:
    target_by_name = {target.qualified_name: target for target in targets}
    source_evidence = source_concept_evidence(profile)
    source_concepts = set(source_evidence["matched_concepts"])
    adjusted = [
        _adjust_candidate(candidate, source_concepts, target_by_name[str(candidate["target"])])
        for candidate in v3_candidates
    ]
    return _tiered_rank(v3_candidates, adjusted)


def suggest_contract_mappings_v4(
    contract,
    source_path,
    *,
    model_name: str = DEFAULT_MODEL_NAME,
    embedding_backend: EmbeddingBackend | None = None,
) -> dict[str, Any]:
    from src.core.mapping.profiler import profile_source_csv
    from src.core.mapping.target_index import build_target_field_index

    profiles, source_meta = profile_source_csv(source_path)
    targets = build_target_field_index(contract)
    backend = embedding_backend or load_embedding_backend(model_name)
    mappings = score_source_fields_v4(profiles, targets, backend)
    body = {
        "_meta": {
            "component": "contract_field_mapping",
            "contract_id": contract.contract_id,
            "contract_version": contract.version,
            "contract_sha256": contract.descriptor_sha256,
            "adapter": contract.adapter,
            "domain": contract.domain,
            **source_meta,
            "target_field_count": len(targets),
            "embedding_model": model_name,
            **metadata(),
        },
        "mappings": mappings,
    }
    return attach_run_info(body)


def _adjust_candidate(
    candidate: dict[str, Any],
    source_concepts: set[str],
    target: ContractTargetField,
) -> dict[str, Any]:
    v3_score = float(candidate["score"])
    target_evidence = target_concept_evidence(target)
    target_concepts = set(target_evidence["matched_concepts"])
    evidence = active_interactions(source_concepts, target_concepts, v3_score=v3_score)
    diagnostic_bonus = sum(float(item["bonus_weight"]) * (1.0 - v3_score) for item in evidence if item["tier"] == "diagnostic")
    supportive_bonus = sum(float(item["bonus_weight"]) * (1.0 - v3_score) for item in evidence if item["tier"] == "supportive")
    adjusted_score = min(1.0, max(0.0, v3_score + diagnostic_bonus + supportive_bonus))
    activated = [str(item["interaction_id"]) for item in evidence]
    return {
        **candidate,
        "v3_score": v3_score,
        "activated_interactions": activated,
        "interaction_evidence": evidence,
        "diagnostic_bonus": round(diagnostic_bonus, 6),
        "supportive_bonus": round(supportive_bonus, 6),
        "interaction_adjusted_score": round(adjusted_score, 4),
        "_interaction_sort_score": adjusted_score,
        "top1_eligible": "routing_to_routing" in activated,
        "top1_selection_reason": "pending",
        "score": round(adjusted_score, 4),
    }


def _tiered_rank(
    v3_candidates: list[dict[str, Any]],
    adjusted_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    v3_ranked = sorted(v3_candidates, key=lambda item: int(item["rank"]))
    v3_top1 = str(v3_ranked[0]["target"])
    by_target = {str(candidate["target"]): candidate for candidate in adjusted_candidates}
    v3_top1_adjusted = float(by_target[v3_top1]["_interaction_sort_score"])
    diagnostic_challengers = sorted(
        (
            candidate
            for candidate in adjusted_candidates
            if "routing_to_routing" in candidate["activated_interactions"]
            and float(candidate["_interaction_sort_score"]) > v3_top1_adjusted
        ),
        key=lambda item: (-float(item["_interaction_sort_score"]), str(item["target"])),
    )
    selected_top1 = str(diagnostic_challengers[0]["target"]) if diagnostic_challengers else v3_top1
    selection_reason = (
        "diagnostic_adjusted_score_strictly_exceeded_v3_top1"
        if diagnostic_challengers
        else "v3_top1_locked_no_diagnostic_challenger"
    )
    remaining = [
        candidate
        for candidate in adjusted_candidates
        if str(candidate["target"]) != selected_top1
    ]
    ranked = [
        {**by_target[selected_top1], "top1_selection_reason": selection_reason},
        *sorted(remaining, key=lambda item: (-float(item["_interaction_sort_score"]), str(item["target"]))),
    ]
    return [
        _public_candidate({**candidate, "rank": index + 1})
        for index, candidate in enumerate(ranked)
    ]


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(candidate)
    cleaned.pop("_interaction_sort_score", None)
    if cleaned["rank"] != 1 and cleaned["top1_selection_reason"] == "pending":
        cleaned["top1_selection_reason"] = "not_selected_for_top1"
    return cleaned


def _candidate_from_dict(candidate: dict[str, Any]) -> MappingCandidate:
    return MappingCandidate(
        target=str(candidate["target"]),
        rank=int(candidate["rank"]),
        score=float(candidate["score"]),
        semantic_score=float(candidate["semantic_score"]),
        fuzzy_score=float(candidate["fuzzy_score"]),
        alias_hit=bool(candidate["alias_hit"]),
        alias_source=candidate["alias_source"],
        lexical_overlap=float(candidate["lexical_overlap"]),
        type_gate=float(candidate["type_gate"]),
        warnings=tuple(candidate["warnings"]),
    )


def metadata() -> dict[str, Any]:
    return {
        "experimental": True,
        "scorer_variant": SCORER_ID,
        "scorer_id": SCORER_ID,
        "feature_version": FEATURE_VERSION,
        "parent_scorer": PARENT_SCORER_ID,
        "production_scorer_modified": False,
        "ground_truth_used": False,
        "ground_truth_used_for_candidate_generation": False,
        "ground_truth_used_for_concept_extraction": False,
        "ground_truth_used_for_interaction_activation": False,
        "ground_truth_used_for_tier_decision": False,
        "ground_truth_used_for_scoring": False,
        "interaction_configuration": interaction_metadata(),
    }
