from __future__ import annotations

from typing import Any

from src.core.mapping.models import ContractTargetField, MappingCandidate, SourceFieldProfile
from src.core.mapping.resource_context import (
    ANCHOR_MARGIN_MIN,
    ANCHOR_SCORE_MIN,
    CONTEXT_WINDOW,
    FEATURE_VERSION,
    RESOURCE_CONTEXT_BONUS_WEIGHT,
    RESOURCE_SUPPORT_MIN,
    resource_context_for_index,
    resource_name,
)
from src.core.mapping.scorer import (
    TOP_N_CANDIDATES,
    EmbeddingBackend,
    _basis,
    _status,
)
from src.core.mapping.scorer_v2 import SCORER_ID as VALUE_PATTERN_SCORER_ID
from src.core.mapping.scorer_v2 import score_all_candidates_v2


SCORER_ID = "target_context_v3"
PARENT_SCORER_ID = VALUE_PATTERN_SCORER_ID


def score_source_fields_v3(
    profiles: list[SourceFieldProfile],
    targets: list[ContractTargetField],
    backend: EmbeddingBackend,
) -> list[dict[str, Any]]:
    source_fields = [profile.name for profile in profiles]
    all_candidates = {
        profile.name: score_all_candidates_v2(profile, targets, backend)
        for profile in profiles
    }
    return [
        score_source_field_v3(
            profile,
            all_candidates[profile.name],
            resource_context_for_index(source_fields, all_candidates, index),
        )
        for index, profile in enumerate(profiles)
    ]


def score_source_field_v3(
    profile: SourceFieldProfile,
    v2_candidates: list[dict[str, Any]],
    resource_context: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    ranked = score_all_candidates_v3(profile, v2_candidates, resource_context)
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


def score_all_candidates_v3(
    profile: SourceFieldProfile,
    v2_candidates: list[dict[str, Any]],
    resource_context: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the complete, deterministic V3 candidate list.

    Sorting happens before the Top-3 truncation used by
    score_source_field_v3(), which reuses this API and then slices the
    suggestion candidates. Inputs are not modified; returned candidate dicts
    are fresh objects safe for callers to use. Ordering is by (-score, target).
    This supports experimental downstream rerankers without changing the V3
    suggestion contract.
    """
    del profile
    ranked = sorted(
        (_adjust_candidate(candidate, resource_context) for candidate in v2_candidates),
        key=lambda item: (-float(item["score"]), str(item["target"])),
    )
    return [{**candidate, "rank": index + 1} for index, candidate in enumerate(ranked)]


def _adjust_candidate(candidate: dict[str, Any], resource_context: dict[str, dict[str, Any]]) -> dict[str, Any]:
    target_resource = resource_name(str(candidate["target"]))
    active = resource_context.get(target_resource)
    context_score = float(active["support"]) if active else 0.0
    v2_score = float(candidate["score"])
    type_gate = float(candidate["type_gate"])
    if context_score == 0.0:
        final_score = v2_score
    else:
        final_score = round(
            min(1.0, max(0.0, v2_score + RESOURCE_CONTEXT_BONUS_WEIGHT * context_score * type_gate * (1 - v2_score))),
            4,
        )
    return {
        **candidate,
        "v2_score": v2_score,
        "resource_context_score": context_score,
        "resource_context_support": context_score if active else 0.0,
        "resource_context_evidence": list(active["evidence"]) if active else [],
        "score": final_score,
    }


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
        "feature_version": FEATURE_VERSION,
        "parent_scorer": PARENT_SCORER_ID,
        "production_scorer_modified": False,
        "ground_truth_used": False,
        "context_window": CONTEXT_WINDOW,
        "anchor_score_min": ANCHOR_SCORE_MIN,
        "anchor_margin_min": ANCHOR_MARGIN_MIN,
        "resource_support_min": RESOURCE_SUPPORT_MIN,
        "resource_context_bonus_weight": RESOURCE_CONTEXT_BONUS_WEIGHT,
    }
