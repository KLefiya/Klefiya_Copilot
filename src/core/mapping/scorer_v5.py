from __future__ import annotations

from typing import Any

from src.core.mapping.identifier_interactions import (
    FEATURE_VERSION,
    identifier_interaction_evidence,
    metadata as identifier_interaction_metadata,
)
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
from src.core.mapping.scorer_v3 import score_all_candidates_v3
from src.core.mapping.scorer_v4 import SCORER_ID as PRECISION_TIERED_V4_SCORER_ID
from src.core.mapping.scorer_v4 import score_all_candidates_v4
from src.tools.data_profile import attach_run_info


SCORER_ID = "precision_tiered_v5"
PARENT_SCORER_ID = PRECISION_TIERED_V4_SCORER_ID


def score_source_fields_v5(
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
        score_source_field_v5(
            profile,
            score_all_candidates_v4(profile, all_v3_candidates[profile.name], targets),
            targets,
        )
        for profile in profiles
    ]


def score_source_field_v5(
    profile: SourceFieldProfile,
    v4_candidates: list[dict[str, Any]],
    targets: list[ContractTargetField],
) -> dict[str, Any]:
    ranked = score_all_candidates_v5(profile, v4_candidates, targets)
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


def score_all_candidates_v5(
    profile: SourceFieldProfile,
    v4_candidates: list[dict[str, Any]],
    targets: list[ContractTargetField],
) -> list[dict[str, Any]]:
    target_by_name = {target.qualified_name: target for target in targets}
    adjusted = [
        _adjust_candidate(profile, candidate, target_by_name[str(candidate["target"])])
        for candidate in v4_candidates
    ]
    return _tiered_rank(v4_candidates, adjusted)


def suggest_contract_mappings_v5(
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
    mappings = score_source_fields_v5(profiles, targets, backend)
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
    profile: SourceFieldProfile,
    candidate: dict[str, Any],
    target: ContractTargetField,
) -> dict[str, Any]:
    v4_score = float(candidate["score"])
    evidence = identifier_interaction_evidence(profile, target, v4_score=v4_score)
    bonus = round(sum(float(item["bonus"]) for item in evidence), 6)
    adjusted_score = round(min(1.0, max(0.0, v4_score + bonus)), 4)
    return {
        **candidate,
        "v4_score": v4_score,
        "identifier_interaction_evidence": evidence,
        "identifier_bonus": bonus,
        "identifier_adjusted_score": adjusted_score,
        "v5_top1_eligible": bool(evidence),
        "v5_top1_selection_reason": "pending",
        "score": adjusted_score,
    }


def _tiered_rank(
    v4_candidates: list[dict[str, Any]],
    adjusted_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    v4_ranked = sorted(v4_candidates, key=lambda item: int(item["rank"]))
    v4_top1 = str(v4_ranked[0]["target"])
    by_target = {str(candidate["target"]): candidate for candidate in adjusted_candidates}
    v4_top1_adjusted = float(by_target[v4_top1]["identifier_adjusted_score"])
    challengers = sorted(
        (
            candidate
            for candidate in adjusted_candidates
            if candidate["v5_top1_eligible"]
            and str(candidate["target"]) != v4_top1
            and float(candidate["identifier_adjusted_score"]) > v4_top1_adjusted
        ),
        key=lambda item: (-float(item["identifier_adjusted_score"]), str(item["target"])),
    )
    selected_top1 = str(challengers[0]["target"]) if challengers else v4_top1
    reason = (
        "identifier_adjusted_score_strictly_exceeded_v4_top1"
        if challengers
        else "v4_top1_retained_no_identifier_challenger"
    )
    remaining = [
        candidate
        for candidate in adjusted_candidates
        if str(candidate["target"]) != selected_top1
    ]
    ranked = [
        {**by_target[selected_top1], "v5_top1_selection_reason": reason},
        *sorted(remaining, key=lambda item: (-float(item["identifier_adjusted_score"]), str(item["target"]))),
    ]
    return [
        _public_candidate({**candidate, "rank": index + 1})
        for index, candidate in enumerate(ranked)
    ]


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(candidate)
    if cleaned["rank"] != 1 and cleaned["v5_top1_selection_reason"] == "pending":
        cleaned["v5_top1_selection_reason"] = "not_selected_for_top1"
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
        "ground_truth_used_for_scoring": False,
        "interaction_configuration": identifier_interaction_metadata(),
    }
