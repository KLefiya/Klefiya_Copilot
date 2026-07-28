from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.core.contracts.loader import PROJECT_ROOT, LoadedMigrationContract
from src.core.mapping.profiler import profile_source_csv
from src.core.mapping.scorer import (
    DEFAULT_MODEL_NAME,
    HIGH_CONFIDENCE,
    MEDIUM_CONFIDENCE,
    NO_MATCH_THRESHOLD,
    ALIAS_CONFIDENCE_FLOOR,
    TYPE_GATE_FLOOR,
    TOP_N_CANDIDATES,
    EmbeddingBackend,
    load_embedding_backend,
    score_source_field,
)
from src.core.mapping.target_index import build_target_field_index
from src.tools.data_profile import attach_run_info


def _project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _status_counts(mappings: list[dict[str, Any]]) -> dict[str, int]:
    return {
        status: sum(1 for item in mappings if item["status"] == status)
        for status in (
            "suggested",
            "needs_review",
            "possible_false_friend",
            "no_confident_target",
        )
    }


def suggest_contract_mappings(
    contract: LoadedMigrationContract,
    source_path: Path,
    *,
    model_name: str = DEFAULT_MODEL_NAME,
    embedding_backend: EmbeddingBackend | None = None,
) -> dict[str, Any]:
    profiles, source_meta = profile_source_csv(source_path)
    targets = build_target_field_index(contract)
    backend = embedding_backend or load_embedding_backend(model_name)
    suggestions = [
        score_source_field(profile, targets, backend).to_dict()
        for profile in profiles
    ]
    status_counts = _status_counts(suggestions)
    recommended_targets = {
        item["recommendation"]
        for item in suggestions
        if item["recommendation"] is not None
    }
    alias_based = sum(1 for item in suggestions if item["mapping_basis"] == "alias")
    semantic_based = sum(1 for item in suggestions if item["mapping_basis"] == "semantic")
    unmapped = sorted(
        field.qualified_name
        for field in targets
        if field.qualified_name not in recommended_targets
    )
    body = {
        "_meta": {
            "component": "contract_field_mapping",
            "contract_id": contract.contract_id,
            "contract_version": contract.version,
            "contract_sha256": contract.descriptor_sha256,
            "adapter": contract.adapter,
            "domain": contract.domain,
            "source_path": source_meta["source_path"],
            "source_sha256": source_meta["source_sha256"],
            "source_row_count": source_meta["source_row_count"],
            "source_field_count": source_meta["source_field_count"],
            "target_field_count": len(targets),
            "embedding_model": model_name,
            "thresholds": {
                "high_confidence": HIGH_CONFIDENCE,
                "medium_confidence": MEDIUM_CONFIDENCE,
                "no_match_threshold": NO_MATCH_THRESHOLD,
                "alias_confidence_floor": ALIAS_CONFIDENCE_FLOOR,
                "type_gate_floor": TYPE_GATE_FLOOR,
                "top_n_candidates": TOP_N_CANDIDATES,
            },
            "read_only": True,
            "ground_truth_used": False,
        },
        "summary": {
            **status_counts,
            "alias_based": alias_based,
            "semantic_based": semantic_based,
            "target_coverage": round(len(recommended_targets) / len(targets), 4) if targets else 0.0,
        },
        "mappings": suggestions,
        "unmapped_target_fields": unmapped,
    }
    return attach_run_info(body)


def write_mapping_report(report: dict[str, Any], output_path: Path) -> None:
    output = Path(output_path).resolve()
    output.relative_to(PROJECT_ROOT)
    next_report = deepcopy(report)
    if output.exists():
        try:
            previous = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
        previous_run = previous.get("_run_info", {})
        next_run = next_report.get("_run_info", {})
        if (
            previous_run.get("content_sha256") == next_run.get("content_sha256")
            and previous_run.get("generated_at")
        ):
            next_report["_run_info"]["generated_at"] = previous_run["generated_at"]
    if os.environ.get("CARVEOPS_OMIT_TIMESTAMP") == "1":
        next_report.get("_run_info", {}).pop("generated_at", None)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(next_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
