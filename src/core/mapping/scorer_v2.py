from __future__ import annotations

from dataclasses import replace

from src.core.mapping.models import ContractTargetField, MappingCandidate, SourceFieldProfile
from src.core.mapping.scorer import (
    ALIAS_CONFIDENCE_FLOOR,
    DEFAULT_MODEL_NAME,
    TOP_N_CANDIDATES,
    EmbeddingBackend,
    _alias_hit,
    _basis,
    _embedding_scores,
    _fuzzy_score,
    _lexical_overlap,
    _status,
    _target_text,
    _type_gate,
    expanded_text,
)
from src.core.mapping.value_patterns import VALUE_PATTERN_FEATURE_VERSION, value_pattern_evidence


SCORER_ID = "value_pattern_v2"
VALUE_PATTERN_BONUS_WEIGHT = 0.20


def score_source_field_v2(
    profile: SourceFieldProfile,
    targets: list[ContractTargetField],
    backend: EmbeddingBackend,
) -> dict[str, object]:
    source_text = expanded_text(profile.name)
    semantic_scores = _embedding_scores(backend, source_text, [_target_text(target) for target in targets])
    candidates: list[MappingCandidate] = []
    candidate_extras: dict[str, dict[str, object]] = {}
    for target, semantic_score in zip(targets, semantic_scores):
        alias_hit, alias_source = _alias_hit(profile.name, target)
        fuzzy_score = _fuzzy_score(profile.name, target)
        lexical_overlap = _lexical_overlap(profile.name, target)
        type_gate, warnings = _type_gate(profile, target)
        base_blended = (0.58 * semantic_score) + (0.27 * fuzzy_score) + (0.15 * lexical_overlap)
        if alias_hit:
            base_blended = max(base_blended, ALIAS_CONFIDENCE_FLOOR)
        baseline_score = round(min(1.0, max(0.0, base_blended * type_gate)), 4)
        pattern = value_pattern_evidence(profile, target)
        pattern_adjusted_blended = base_blended + (VALUE_PATTERN_BONUS_WEIGHT * pattern.score * (1 - base_blended))
        score = round(min(1.0, max(0.0, pattern_adjusted_blended * type_gate)), 4)
        candidate = MappingCandidate(
            target=target.qualified_name,
            rank=0,
            score=score,
            semantic_score=round(semantic_score, 4),
            fuzzy_score=round(fuzzy_score, 4),
            alias_hit=alias_hit,
            alias_source=alias_source,
            lexical_overlap=round(lexical_overlap, 4),
            type_gate=round(type_gate, 4),
            warnings=warnings,
        )
        candidates.append(candidate)
        candidate_extras[target.qualified_name] = {
            "base_score": round(base_blended, 6),
            "base_blended": round(base_blended, 6),
            "baseline_score": baseline_score,
            "pattern_adjusted_blended": round(pattern_adjusted_blended, 6),
            "value_pattern_score": pattern.score,
            "value_pattern_support": pattern.support,
            "value_pattern_evidence": list(pattern.evidence),
        }
    ranked = sorted(candidates, key=lambda item: (-item.score, item.target))
    ranked = [replace(candidate, rank=index + 1) for index, candidate in enumerate(ranked)]
    top = tuple(ranked[:TOP_N_CANDIDATES])
    best = top[0]
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
        "top_candidates": [
            {**candidate.to_dict(), **candidate_extras[candidate.target]}
            for candidate in top
        ],
    }
