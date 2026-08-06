from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.contracts.loader import PROJECT_ROOT
from src.core.hashing import canonical_json_content_sha256, normalized_text_sha256


HASH_MODE = "normalized_text_sha256_v1"
ALLOWED_PROVENANCE_ONLY_ENGINE_FILES = {
    "src/core/mapping/profiler.py",
    "src/core/mapping/engine.py",
}


class ProtocolLockError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def project_relative(path: Path, *, project_root: Path = PROJECT_ROOT) -> str:
    return Path(path).resolve().relative_to(project_root).as_posix()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProtocolLockError("invalid_json", f"{label} is not valid JSON", {"path": str(path)}) from exc
    if not isinstance(data, dict):
        raise ProtocolLockError("invalid_json_object", f"{label} must be a JSON object", {"path": str(path)})
    return data


def require_equal(code: str, label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise ProtocolLockError(code, f"{label} mismatch", {"actual": actual, "expected": expected})


def require_normalized_file_sha(path: Path, expected: str, *, label: str) -> None:
    actual = normalized_text_sha256(path)
    if actual != expected:
        raise ProtocolLockError(
            "hash_mismatch",
            f"{label} normalized-text SHA mismatch",
            {"path": project_relative(path), "actual": actual, "expected": expected},
        )


def engine_file_sha_mismatches(engine_files: dict[str, Any], *, project_root: Path = PROJECT_ROOT) -> list[dict[str, str]]:
    mismatches: list[dict[str, str]] = []
    for rel_path, expected_sha in sorted(engine_files.items()):
        actual = normalized_text_sha256(project_root / rel_path)
        expected = str(expected_sha)
        if actual != expected:
            mismatches.append(
                {
                    "path": rel_path,
                    "actual": actual,
                    "expected": expected,
                }
            )
    return mismatches


def validate_base_inputs(lock: dict[str, Any], *, project_root: Path = PROJECT_ROOT) -> None:
    base_dir = project_root / "data" / "examples" / "blind" / "erpnext_item_price"
    require_normalized_file_sha(
        project_root / "contracts" / "erpnext_item_price_reference" / "datapackage.yaml",
        str(lock.get("contract_sha256")),
        label="contract",
    )
    require_normalized_file_sha(base_dir / "source_product_catalog.csv", str(lock.get("source_sha256")), label="source")
    require_normalized_file_sha(base_dir / "ground_truth.json", str(lock.get("ground_truth_sha256")), label="ground_truth")
    require_normalized_file_sha(
        project_root / "references" / "erpnext_item_price" / "upstream_reference.json",
        str(lock.get("upstream_reference_sha256")),
        label="upstream_reference",
    )


def validate_historical_protocol_lock(lock_path: Path, *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    lock_abs = Path(lock_path).resolve()
    lock = load_json(lock_abs, "protocol_lock")
    validate_base_inputs(lock, project_root=project_root)
    engine_files = lock.get("engine_files")
    if not isinstance(engine_files, dict) or not engine_files:
        raise ProtocolLockError("invalid_engine_files", "protocol lock requires engine_files")
    mismatches = engine_file_sha_mismatches(engine_files, project_root=project_root)
    if mismatches:
        changed = ", ".join(f"engine:{item['path']}" for item in mismatches)
        raise ProtocolLockError(
            "hash_mismatch",
            f"engine normalized-text SHA mismatch: {changed}",
            {"changed_engine_files": mismatches},
        )
    return {
        "validation": "valid",
        "mode": "historical_lock",
        "protocol_lock_path": project_relative(lock_abs, project_root=project_root),
        "protocol_lock_normalized_text_sha256": normalized_text_sha256(lock_abs),
        "hash_mode": HASH_MODE,
    }


def validate_amendment_shape(amendment: dict[str, Any]) -> None:
    run_info = amendment.get("_run_info")
    if not isinstance(run_info, dict) or not isinstance(run_info.get("content_sha256"), str):
        raise ProtocolLockError("amendment_content_sha_missing", "amendment is missing _run_info.content_sha256")
    expected_content_sha = canonical_json_content_sha256(amendment)
    if run_info["content_sha256"] != expected_content_sha:
        raise ProtocolLockError(
            "amendment_content_sha_mismatch",
            "amendment _run_info.content_sha256 does not match canonical content",
            {"actual": run_info["content_sha256"], "expected": expected_content_sha},
        )
    require_equal("invalid_amendment_version", "amendment_version", amendment.get("amendment_version"), "1.0")
    require_equal(
        "invalid_amendment_purpose",
        "purpose",
        amendment.get("purpose"),
        "cross_platform_provenance_normalization",
    )
    require_equal("invalid_hash_mode", "hash_mode", amendment.get("hash_mode"), HASH_MODE)
    require_equal("invalid_change_class", "change_class", amendment.get("change_class"), "provenance_only")
    require_equal("scoring_logic_changed", "scoring_logic_changed", amendment.get("scoring_logic_changed"), False)
    require_equal(
        "ground_truth_used_for_mapping",
        "ground_truth_used_for_mapping",
        amendment.get("ground_truth_used_for_mapping"),
        False,
    )
    require_equal(
        "not_locked_before_maintenance_replay",
        "locked_before_maintenance_replay",
        amendment.get("locked_before_maintenance_replay"),
        True,
    )
    if amendment.get("locked_before_first_mapping") is True:
        raise ProtocolLockError(
            "invalid_first_mapping_claim",
            "compatibility amendment must not claim locked_before_first_mapping=true",
        )


def validate_effective_protocol_lock(
    lock_path: Path,
    amendment_path: Path,
    *,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    lock_abs = Path(lock_path).resolve()
    amendment_abs = Path(amendment_path).resolve()
    lock = load_json(lock_abs, "protocol_lock")
    amendment = load_json(amendment_abs, "protocol_amendment")
    validate_amendment_shape(amendment)

    require_equal(
        "base_lock_path_mismatch",
        "base_lock_path",
        amendment.get("base_lock_path"),
        project_relative(lock_abs, project_root=project_root),
    )
    require_equal(
        "base_lock_sha_mismatch",
        "base_lock_normalized_text_sha256",
        amendment.get("base_lock_normalized_text_sha256"),
        normalized_text_sha256(lock_abs),
    )
    unchanged = amendment.get("unchanged_protocol_values")
    if not isinstance(unchanged, dict):
        raise ProtocolLockError("invalid_unchanged_protocol_values", "amendment requires unchanged_protocol_values")
    for key in (
        "embedding_model",
        "thresholds",
        "contract_sha256",
        "source_sha256",
        "ground_truth_sha256",
        "upstream_reference_sha256",
        "aliases_present",
    ):
        require_equal(f"unchanged_{key}_mismatch", f"unchanged_protocol_values.{key}", unchanged.get(key), lock.get(key))

    validate_base_inputs(lock, project_root=project_root)
    engine_files = lock.get("engine_files")
    if not isinstance(engine_files, dict) or not ALLOWED_PROVENANCE_ONLY_ENGINE_FILES <= set(engine_files):
        raise ProtocolLockError("invalid_engine_files", "base lock does not include the allowed provenance-only files")
    changes = amendment.get("allowed_engine_file_changes")
    if not isinstance(changes, list) or not all(isinstance(change, dict) for change in changes):
        raise ProtocolLockError("invalid_allowed_engine_changes", "amendment must list allowed engine file changes")
    changes_by_path = {str(change.get("path")): change for change in changes}
    if set(changes_by_path) != ALLOWED_PROVENANCE_ONLY_ENGINE_FILES or len(changes_by_path) != len(changes):
        raise ProtocolLockError(
            "unexpected_engine_change_path",
            "amendment must allow exactly the provenance-only profiler and engine metadata changes",
            {"actual": sorted(changes_by_path), "expected": sorted(ALLOWED_PROVENANCE_ONLY_ENGINE_FILES)},
        )
    for rel_path in sorted(ALLOWED_PROVENANCE_ONLY_ENGINE_FILES):
        change = changes_by_path[rel_path]
        require_equal(
            "unexpected_engine_change_class",
            f"{rel_path} change_class",
            change.get("change_class"),
            "provenance_only",
        )
        require_equal(
            "engine_before_sha_mismatch",
            f"{rel_path} before SHA",
            change.get("before_normalized_text_sha256"),
            engine_files[rel_path],
        )
        current_sha = normalized_text_sha256(project_root / rel_path)
        require_equal(
            "engine_after_sha_mismatch",
            f"{rel_path} after SHA",
            current_sha,
            change.get("after_normalized_text_sha256"),
        )
    for rel_path, expected_sha in sorted(engine_files.items()):
        if rel_path in ALLOWED_PROVENANCE_ONLY_ENGINE_FILES:
            continue
        require_normalized_file_sha(project_root / rel_path, str(expected_sha), label=f"engine:{rel_path}")

    return {
        "validation": "valid",
        "mode": "effective_lock_with_compatibility_amendment",
        "protocol_lock_path": project_relative(lock_abs, project_root=project_root),
        "protocol_lock_normalized_text_sha256": normalized_text_sha256(lock_abs),
        "protocol_amendment_path": project_relative(amendment_abs, project_root=project_root),
        "protocol_amendment_content_sha256": amendment["_run_info"]["content_sha256"],
        "hash_mode": HASH_MODE,
        "allowed_engine_file_changes": [changes_by_path[path] for path in sorted(changes_by_path)],
    }
