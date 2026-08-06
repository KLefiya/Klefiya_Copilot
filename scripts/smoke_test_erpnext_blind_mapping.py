from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.hashing import provenance_text_or_raw_sha256


CONTRACT = PROJECT_ROOT / "contracts" / "erpnext_item_price_reference" / "datapackage.yaml"
DATA_ROOT = PROJECT_ROOT / "data" / "examples" / "blind" / "erpnext_item_price"
SOURCE = DATA_ROOT / "source_product_catalog.csv"
TRUTH = DATA_ROOT / "ground_truth.json"
LOCK = DATA_ROOT / "blind_protocol_lock.json"
AMENDMENT = DATA_ROOT / "blind_protocol_compatibility_amendment_v1.json"
MAPPING = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_mapping.json"
EVALUATION = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_evaluation.json"


def _sha(path: Path) -> str:
    return provenance_text_or_raw_sha256(path)


def _safe_output_root(path: Path) -> Path:
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError("--output-root must stay inside the project root") from exc
    if ".." in Path(path).parts:
        raise ValueError("--output-root must not contain path escapes")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _run(args: list[str]) -> None:
    result = subprocess.run(args, cwd=PROJECT_ROOT, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr + result.stdout)


def _has_absolute_path(value) -> bool:
    if isinstance(value, dict):
        return any(_has_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_absolute_path(item) for item in value)
    if isinstance(value, str):
        return bool(re.match(r"^[A-Za-z]:[\\/]", value))
    return False


def run_smoke(output_root: Path) -> int:
    mapping_path = output_root / "erpnext_item_price_blind_mapping.json"
    evaluation_path = output_root / "erpnext_item_price_blind_evaluation.json"
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    checks = {
        "lock_contract_sha": lock["contract_sha256"] == _sha(CONTRACT),
        "lock_source_sha": lock["source_sha256"] == _sha(SOURCE),
        "lock_truth_sha": lock["ground_truth_sha256"] == _sha(TRUTH),
        "aliases": lock["aliases_present"] is False,
        "locked_first": lock["locked_before_first_mapping"] is True,
    }
    first_mapping_sha = None
    first_eval_sha = None
    _run([
        sys.executable,
        "src/tools/suggest_contract_mappings.py",
        "--contract", str(CONTRACT),
        "--data-root", str(DATA_ROOT),
        "--source", str(SOURCE),
        "--output", str(mapping_path),
    ])
    _run([
        sys.executable,
        "src/tools/evaluate_blind_multitarget_mapping.py",
        "--mapping-report", str(mapping_path),
        "--ground-truth", str(TRUTH),
        "--protocol-lock", str(LOCK),
        "--protocol-amendment", str(AMENDMENT),
        "--output", str(evaluation_path),
    ])
    first_mapping_sha = json.loads(mapping_path.read_text(encoding="utf-8"))["_run_info"]["content_sha256"]
    first_eval_sha = json.loads(evaluation_path.read_text(encoding="utf-8"))["_run_info"]["content_sha256"]
    _run([
        sys.executable,
        "src/tools/suggest_contract_mappings.py",
        "--contract", str(CONTRACT),
        "--data-root", str(DATA_ROOT),
        "--source", str(SOURCE),
        "--output", str(mapping_path),
    ])
    _run([
        sys.executable,
        "src/tools/evaluate_blind_multitarget_mapping.py",
        "--mapping-report", str(mapping_path),
        "--ground-truth", str(TRUTH),
        "--protocol-lock", str(LOCK),
        "--protocol-amendment", str(AMENDMENT),
        "--output", str(evaluation_path),
    ])
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    checks.update(
        {
            "mapping_stable": first_mapping_sha == mapping["_run_info"]["content_sha256"],
            "evaluation_stable": first_eval_sha == evaluation["_run_info"]["content_sha256"],
            "amendment_applied": evaluation["_meta"]["protocol_amendment_path"] == _relative(AMENDMENT),
            "complete_per_source": len(evaluation["per_source_results"]) == 10,
            "expected_links": evaluation["summary"]["expected_target_links"] == 11,
            "multi_target_fields": evaluation["summary"]["multi_target_source_fields"] == 2,
            "no_target_fields": evaluation["summary"]["no_target_source_fields"] == 1,
            "zero_high_confidence_precision": (
                evaluation["summary"]["high_confidence_predictions"] == 0
                and evaluation["summary"]["high_confidence_source_precision"] is None
                and evaluation["summary"]["high_confidence_source_precision_defined"] is False
            ),
            "no_absolute_paths": not _has_absolute_path(mapping) and not _has_absolute_path(evaluation),
        }
    )
    valid = all(checks.values())
    print("ERPNext blind benchmark: contract-driven")
    print(f"Source fields: {evaluation['summary']['source_fields']}")
    print(f"Mapping status: {evaluation['mapping_status_distribution']}")
    print(f"Source top1 accuracy: {evaluation['summary']['source_top1_accuracy']:.4f}")
    print(f"Top3 target-link recall: {evaluation['summary']['top3_target_link_recall']:.4f}")
    print(f"Multi-target full coverage: {evaluation['summary']['multi_target_full_top3_coverage']:.4f}")
    print(f"No-target accuracy: {evaluation['summary']['no_target_accuracy']:.4f}")
    precision = evaluation["summary"]["high_confidence_source_precision"]
    precision_text = f"{precision:.4f}" if precision is not None else "undefined"
    print(f"High-confidence precision: {precision_text}")
    print(f"Deterministic replay: {'valid' if checks['mapping_stable'] and checks['evaluation_stable'] else 'invalid'}")
    print(f"Protocol lock: {'valid' if checks['lock_contract_sha'] and checks['lock_source_sha'] and checks['lock_truth_sha'] and checks['amendment_applied'] else 'invalid'}")
    print(f"Validation: {'valid' if valid else 'invalid'}")
    return 0 if valid else 1


def _relative(path: Path) -> str:
    return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()


def main(argv: list[str] | None = None) -> int:
    if argv is not None and argv:
        if len(argv) != 2 or argv[0] != "--output-root":
            raise ValueError("Usage: smoke_test_erpnext_blind_mapping.py [--output-root PATH]")
        return run_smoke(_safe_output_root(Path(argv[1])))
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as root:
        return run_smoke(Path(root) / "blind-smoke-output")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
