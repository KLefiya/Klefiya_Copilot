from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


CONTRACT = PROJECT_ROOT / "contracts" / "erpnext_item_price_reference" / "datapackage.yaml"
DATA_ROOT = PROJECT_ROOT / "data" / "examples" / "blind" / "erpnext_item_price"
SOURCE = DATA_ROOT / "source_product_catalog.csv"
TRUTH = DATA_ROOT / "ground_truth.json"
LOCK = DATA_ROOT / "blind_protocol_lock.json"
MAPPING = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_mapping.json"
EVALUATION = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_evaluation.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def main() -> int:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    checks = {
        "lock_contract_sha": lock["contract_sha256"] == _sha(CONTRACT),
        "lock_source_sha": lock["source_sha256"] == _sha(SOURCE),
        "lock_truth_sha": lock["ground_truth_sha256"] == _sha(TRUTH),
        "aliases": lock["aliases_present"] is False,
        "locked_first": lock["locked_before_first_mapping"] is True,
    }
    first_mapping_sha = json.loads(MAPPING.read_text(encoding="utf-8"))["_run_info"]["content_sha256"]
    first_eval_sha = json.loads(EVALUATION.read_text(encoding="utf-8"))["_run_info"]["content_sha256"]
    _run([
        sys.executable,
        "src/tools/suggest_contract_mappings.py",
        "--contract", str(CONTRACT),
        "--data-root", str(DATA_ROOT),
        "--source", str(SOURCE),
        "--output", str(MAPPING),
    ])
    _run([
        sys.executable,
        "src/tools/evaluate_blind_multitarget_mapping.py",
        "--mapping-report", str(MAPPING),
        "--ground-truth", str(TRUTH),
        "--protocol-lock", str(LOCK),
        "--output", str(EVALUATION),
    ])
    mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
    evaluation = json.loads(EVALUATION.read_text(encoding="utf-8"))
    checks.update(
        {
            "mapping_stable": first_mapping_sha == mapping["_run_info"]["content_sha256"],
            "evaluation_stable": first_eval_sha == evaluation["_run_info"]["content_sha256"],
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
    print(f"Protocol lock: {'valid' if checks['lock_contract_sha'] and checks['lock_source_sha'] and checks['lock_truth_sha'] else 'invalid'}")
    print(f"Validation: {'valid' if valid else 'invalid'}")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
