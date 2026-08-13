from __future__ import annotations

from dataclasses import dataclass
from typing import Any


FEATURE_VERSION = "target_resource_context_v1"
CONTEXT_WINDOW = 2
ANCHOR_SCORE_MIN = 0.45
ANCHOR_MARGIN_MIN = 0.05
RESOURCE_SUPPORT_MIN = 0.60
RESOURCE_CONTEXT_BONUS_WEIGHT = 0.10
DISTANCE_WEIGHTS = {1: 1.0, 2: 0.5}


@dataclass(frozen=True)
class ResourceAnchor:
    source_field: str
    resource: str
    score: float
    margin: float


def resource_name(qualified_target: str) -> str:
    return qualified_target.split(".", 1)[0]


def anchor_from_candidates(source_field: str, candidates: list[dict[str, Any]]) -> ResourceAnchor | None:
    if not candidates:
        return None
    top1 = candidates[0]
    top2_score = float(candidates[1].get("score", 0.0)) if len(candidates) > 1 else 0.0
    top1_score = float(top1.get("score", 0.0))
    margin = round(top1_score - top2_score, 4)
    if top1_score < ANCHOR_SCORE_MIN or margin < ANCHOR_MARGIN_MIN:
        return None
    return ResourceAnchor(
        source_field=source_field,
        resource=resource_name(str(top1["target"])),
        score=top1_score,
        margin=margin,
    )


def resource_context_for_index(
    source_fields: list[str],
    ranked_candidates_by_source: dict[str, list[dict[str, Any]]],
    index: int,
) -> dict[str, dict[str, Any]]:
    total_weight = 0.0
    weight_by_resource: dict[str, float] = {}
    evidence_by_resource: dict[str, list[dict[str, Any]]] = {}
    anchor_count = 0

    for neighbor_index in range(max(0, index - CONTEXT_WINDOW), min(len(source_fields), index + CONTEXT_WINDOW + 1)):
        if neighbor_index == index:
            continue
        distance = abs(neighbor_index - index)
        distance_weight = DISTANCE_WEIGHTS[distance]
        neighbor = source_fields[neighbor_index]
        anchor = anchor_from_candidates(neighbor, ranked_candidates_by_source.get(neighbor, []))
        if anchor is None:
            continue
        anchor_count += 1
        total_weight += distance_weight
        weight_by_resource[anchor.resource] = weight_by_resource.get(anchor.resource, 0.0) + distance_weight
        evidence_by_resource.setdefault(anchor.resource, []).append(
            {
                "neighbor_source_field": neighbor,
                "distance": distance,
                "anchor_resource": anchor.resource,
                "anchor_score": anchor.score,
                "anchor_margin": anchor.margin,
                "distance_weight": distance_weight,
            }
        )

    if anchor_count < 2 or total_weight == 0.0:
        return {}

    active: dict[str, dict[str, Any]] = {}
    for resource in sorted(weight_by_resource):
        support = round(weight_by_resource[resource] / total_weight, 4)
        if support >= RESOURCE_SUPPORT_MIN:
            active[resource] = {
                "support": support,
                "anchor_count": anchor_count,
                "evidence": evidence_by_resource[resource],
            }
    return active
