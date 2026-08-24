from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.contracts.loader import PROJECT_ROOT, load_migration_contract
from src.core.mapping.target_index import build_target_field_index


FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "schema_matching_identifier_robustness_v1.json"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
BASELINE_SCORER = "baseline"
V4_SCORER = "precision_tiered_v4"
V5_SCORER = "precision_tiered_v5"
SCORERS = (BASELINE_SCORER, V4_SCORER, V5_SCORER)
TAXONOMY = (
    "identifier_alias_confusion",
    "abbreviation_confusion",
    "formatting_regression",
    "ambiguous_generic_name",
    "false_positive_no_target",
    "correct_target_below_top3",
    "correct_target_in_top3_not_top1",
    "over_abstention",
)
CONTRACT_REGISTRY = {
    "generic-customer": {
        "contract_path": PROJECT_ROOT / "contracts" / "generic_customer" / "datapackage.yaml",
        "data_root": PROJECT_ROOT / "data" / "examples" / "generic_customer",
    },
    "supplier-reference": {
        "contract_path": PROJECT_ROOT / "contracts" / "sap_supplier_reference" / "datapackage.yaml",
        "data_root": PROJECT_ROOT / "data" / "examples" / "sap_supplier_reference",
    },
    "erpnext-item-price": {
        "contract_path": PROJECT_ROOT / "contracts" / "erpnext_item_price_reference" / "datapackage.yaml",
        "data_root": PROJECT_ROOT / "data" / "examples" / "blind" / "erpnext_item_price",
    },
}


class IdentifierRobustnessError(RuntimeError):
    pass


def fixture_raw_sha256(path: Path = FIXTURE_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_fixture(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    validate_fixture(fixture)
    return fixture


def validate_fixture(fixture: dict[str, Any]) -> None:
    meta = fixture.get("_meta", {})
    if meta.get("fixture_id") != "schema_matching_identifier_robustness_v1":
        raise IdentifierRobustnessError("Unexpected identifier robustness fixture id.")
    if not meta.get("diagnostic") or meta.get("formal_benchmark_artifact"):
        raise IdentifierRobustnessError("Identifier robustness fixture must be diagnostic and non-formal.")
    scenarios = fixture.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise IdentifierRobustnessError("Fixture scenarios must be a non-empty list.")
    seen_case_ids: set[str] = set()
    for scenario in scenarios:
        contract_id = str(scenario.get("contract_id", ""))
        allowlist = contract_target_fields(contract_id)
        cases = scenario.get("cases")
        if not isinstance(cases, list) or not cases:
            raise IdentifierRobustnessError(f"{contract_id} must contain cases.")
        source_fields: set[str] = set()
        for case in cases:
            case_id = str(case.get("case_id", ""))
            if not case_id or case_id in seen_case_ids:
                raise IdentifierRobustnessError(f"Duplicate or missing case_id: {case_id}")
            seen_case_ids.add(case_id)
            source_field = str(case.get("source_field", ""))
            if not source_field or source_field in source_fields:
                raise IdentifierRobustnessError(f"Duplicate or missing source_field in {contract_id}: {source_field}")
            source_fields.add(source_field)
            expected_targets = case.get("expected_targets")
            if not isinstance(expected_targets, list) or not all(isinstance(item, str) for item in expected_targets):
                raise IdentifierRobustnessError(f"{case_id} expected_targets must be a string list.")
            if bool(case.get("expected_no_target")) != (len(expected_targets) == 0):
                raise IdentifierRobustnessError(f"{case_id} expected_no_target disagrees with expected_targets.")
            unknown = sorted(target for target in expected_targets if target not in allowlist)
            if unknown:
                raise IdentifierRobustnessError(f"{case_id} has targets outside {contract_id}: {unknown}")
            samples = case.get("sample_values")
            if not isinstance(samples, list) or not samples or not all(isinstance(item, str) for item in samples):
                raise IdentifierRobustnessError(f"{case_id} sample_values must be non-empty strings.")


def contract_target_fields(contract_id: str) -> tuple[str, ...]:
    spec = CONTRACT_REGISTRY.get(contract_id)
    if spec is None:
        raise IdentifierRobustnessError(f"Unknown diagnostic contract id: {contract_id}")
    contract = load_migration_contract(spec["contract_path"], spec["data_root"])
    return tuple(field.qualified_name for field in build_target_field_index(contract))


def category_counts(fixture: dict[str, Any]) -> dict[str, int]:
    counter = Counter(case["category"] for scenario in fixture["scenarios"] for case in scenario["cases"])
    return dict(sorted(counter.items()))


def contract_counts(fixture: dict[str, Any]) -> dict[str, int]:
    return {
        scenario["contract_id"]: len(scenario["cases"])
        for scenario in sorted(fixture["scenarios"], key=lambda item: item["contract_id"])
    }


def build_source_csv(scenario: dict[str, Any], output_dir: Path) -> Path:
    cases = list(scenario["cases"])
    row_count = max(len(case["sample_values"]) for case in cases)
    path = output_dir / f"{scenario['scenario_id']}.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow([case["source_field"] for case in cases])
        for row_index in range(row_count):
            writer.writerow([
                case["sample_values"][row_index % len(case["sample_values"])]
                for case in cases
            ])
    return path


def suggest_runtime_dispatch(contract: Any, source_path: Path, scorer_id: str) -> dict[str, Any]:
    from src.core.mapping.runtime import suggest_runtime_contract_mappings

    return suggest_runtime_contract_mappings(
        contract,
        source_path,
        scorer_id=scorer_id,
        model_name=MODEL_NAME,
    )


def run_runtime_reports(
    fixture: dict[str, Any],
    scorer_id: str,
    *,
    workspace: Path,
) -> dict[str, dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    for scenario in sorted(fixture["scenarios"], key=lambda item: item["scenario_id"]):
        spec = CONTRACT_REGISTRY[str(scenario["contract_id"])]
        contract = load_migration_contract(spec["contract_path"], spec["data_root"])
        source_path = build_source_csv(scenario, workspace)
        reports[scenario["scenario_id"]] = suggest_runtime_dispatch(contract, source_path, scorer_id)
    return reports


def _safe_metric(numerator: int | float, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(float(numerator) / denominator, 4)


def _mapping_by_source(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["source_field"]): item for item in report.get("mappings", [])}


def _top_targets(mapping: dict[str, Any]) -> list[str]:
    return [str(item.get("target")) for item in mapping.get("top_candidates", [])[:3]]


def _expected_rank(expected_targets: list[str], top_targets: list[str]) -> int | None:
    ranks = [top_targets.index(target) + 1 for target in expected_targets if target in top_targets]
    return min(ranks) if ranks else None


def _rank_error_category(case: dict[str, Any], mapping: dict[str, Any], expected_rank: int | None) -> str | None:
    expected_targets = list(case["expected_targets"])
    recommendation = mapping.get("recommendation")
    if case["expected_no_target"]:
        return None if recommendation is None else "false_positive_no_target"
    if recommendation is None:
        return "over_abstention" if expected_rank is not None else "correct_target_below_top3"
    if recommendation in expected_targets:
        return None
    if expected_rank is None:
        return "correct_target_below_top3"
    return "correct_target_in_top3_not_top1"


def _perturbation_error_category(case: dict[str, Any]) -> str:
    category = str(case["category"])
    if category == "identifier_alias":
        return "identifier_alias_confusion"
    if category == "abbreviation":
        return "abbreviation_confusion"
    if category == "formatting":
        return "formatting_regression"
    if category == "ambiguous_generic_name":
        return "ambiguous_generic_name"
    return "false_positive_no_target"


def _empty_counts() -> dict[str, int]:
    return {
        "case_count": 0,
        "target_case_count": 0,
        "no_target_case_count": 0,
        "expected_target_link_count": 0,
        "top1_correct": 0,
        "recall_at_3_hits": 0,
        "mrr_total": 0,
        "no_target_correct": 0,
        "recommendation_count": 0,
        "correct_recommendation_count": 0,
    }


def _finalize_counts(counts: dict[str, int]) -> dict[str, Any]:
    case_count = counts["case_count"]
    target_cases = counts["target_case_count"]
    no_target_cases = counts["no_target_case_count"]
    recommendations = counts["recommendation_count"]
    return {
        "case_count": case_count,
        "target_case_count": target_cases,
        "no_target_case_count": no_target_cases,
        "expected_target_link_count": counts["expected_target_link_count"],
        "top1_accuracy": _safe_metric(counts["top1_correct"], target_cases),
        "recall_at_3": _safe_metric(counts["recall_at_3_hits"], counts["expected_target_link_count"]),
        "mrr": _safe_metric(counts["mrr_total"], target_cases),
        "no_target_accuracy": _safe_metric(counts["no_target_correct"], no_target_cases),
        "coverage": _safe_metric(recommendations, case_count),
        "suggested_precision": _safe_metric(counts["correct_recommendation_count"], recommendations),
    }


def evaluate_scorer(fixture: dict[str, Any], reports: dict[str, dict[str, Any]], scorer_id: str) -> dict[str, Any]:
    overall = _empty_counts()
    by_category_counts: dict[str, dict[str, int]] = {}
    taxonomy_counts = {key: 0 for key in TAXONOMY}
    errors: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    feature_version = None
    embedding_model = MODEL_NAME

    for scenario in sorted(fixture["scenarios"], key=lambda item: item["scenario_id"]):
        report = reports[scenario["scenario_id"]]
        meta = report.get("_meta", {})
        feature_version = feature_version or meta.get("feature_version")
        embedding_model = str(meta.get("embedding_model") or embedding_model)
        mappings = _mapping_by_source(report)
        for mapping in mappings.values():
            status_counts[str(mapping.get("status"))] += 1
        for case in sorted(scenario["cases"], key=lambda item: item["case_id"]):
            category = str(case["category"])
            category_bucket = by_category_counts.setdefault(category, _empty_counts())
            mapping = mappings.get(str(case["source_field"]))
            if mapping is None:
                raise IdentifierRobustnessError(f"Missing runtime mapping for {case['case_id']}")
            expected_targets = list(case["expected_targets"])
            top_targets = _top_targets(mapping)
            top1 = top_targets[0] if top_targets else None
            expected_rank = _expected_rank(expected_targets, top_targets)
            recommendation = mapping.get("recommendation")

            for bucket in (overall, category_bucket):
                bucket["case_count"] += 1
                bucket["recommendation_count"] += 1 if recommendation is not None else 0
                if case["expected_no_target"]:
                    bucket["no_target_case_count"] += 1
                    bucket["no_target_correct"] += 1 if recommendation is None else 0
                else:
                    bucket["target_case_count"] += 1
                    bucket["expected_target_link_count"] += len(expected_targets)
                    bucket["top1_correct"] += 1 if top1 in expected_targets else 0
                    bucket["recall_at_3_hits"] += sum(1 for target in expected_targets if target in top_targets)
                    bucket["mrr_total"] += (1 / expected_rank) if expected_rank else 0
                    bucket["correct_recommendation_count"] += 1 if recommendation in expected_targets else 0

            rank_error = _rank_error_category(case, mapping, expected_rank)
            if rank_error is not None:
                taxonomy_counts[rank_error] += 1
                perturbation_error = _perturbation_error_category(case)
                if perturbation_error != rank_error and perturbation_error in taxonomy_counts:
                    taxonomy_counts[perturbation_error] += 1
                errors.append({
                    "case_id": case["case_id"],
                    "contract": scenario["contract_id"],
                    "source_field": case["source_field"],
                    "category": category,
                    "expected_target": expected_targets[0] if len(expected_targets) == 1 else expected_targets,
                    "predicted_top1": top1,
                    "top3": top_targets,
                    "final_status": mapping.get("status"),
                    "expected_rank": expected_rank,
                    "error_category": rank_error,
                    "perturbation_error_category": perturbation_error,
                    "recommendation": recommendation,
                })

    return {
        "_meta": {
            "scorer": scorer_id,
            "feature_version": feature_version,
            "model_name": embedding_model,
        },
        "overall": _finalize_counts(overall),
        "by_category": {
            category: _finalize_counts(counts)
            for category, counts in sorted(by_category_counts.items())
        },
        "status_counts": dict(sorted(status_counts.items())),
        "needs_review_count": status_counts.get("needs_review", 0),
        "no_confident_target_count": status_counts.get("no_confident_target", 0),
        "error_taxonomy_counts": dict(sorted(taxonomy_counts.items())),
        "errors": sorted(errors, key=lambda item: (item["case_id"], item["error_category"])),
    }


def build_diagnostic_report(
    fixture: dict[str, Any],
    *,
    fixture_path: Path,
    fixture_sha: str,
    reports_by_scorer: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    cases = [case for scenario in fixture["scenarios"] for case in scenario["cases"]]
    return {
        "_meta": {
            "diagnostic": True,
            "formal_benchmark_artifact": False,
            "fixture_path": fixture_path.resolve().relative_to(PROJECT_ROOT).as_posix(),
            "fixture_sha256": fixture_sha,
            "ground_truth_runtime_boundary": "evaluation_only",
            "ground_truth_passed_to_runtime": False,
            "model_name": MODEL_NAME,
            "scorers": list(SCORERS),
        },
        "fixture": {
            "case_count": len(cases),
            "category_counts": category_counts(fixture),
            "contract_counts": contract_counts(fixture),
        },
        "scorers": {
            scorer: evaluate_scorer(fixture, reports_by_scorer[scorer], scorer)
            for scorer in SCORERS
        },
    }


def default_output_path(fixture_sha: str) -> Path:
    return Path(tempfile.gettempdir()) / f"schema_matching_identifier_robustness_v1_{fixture_sha[:12]}.json"


def write_report(report: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_evaluation(fixture_path: Path = FIXTURE_PATH, output_path: Path | None = None) -> tuple[dict[str, Any], Path]:
    fixture = load_fixture(fixture_path)
    fixture_sha = fixture_raw_sha256(fixture_path)
    runtime_root = PROJECT_ROOT / "data" / "runtime" / "identifier_robustness"
    runtime_root.mkdir(parents=True, exist_ok=True)
    reports_by_scorer: dict[str, dict[str, dict[str, Any]]] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="run_", dir=runtime_root) as workspace_name:
            workspace = Path(workspace_name)
            for scorer in SCORERS:
                reports_by_scorer[scorer] = run_runtime_reports(fixture, scorer, workspace=workspace)
    finally:
        if runtime_root.exists():
            shutil.rmtree(runtime_root)
        data_runtime = PROJECT_ROOT / "data" / "runtime"
        if data_runtime.exists() and not any(data_runtime.iterdir()):
            data_runtime.rmdir()

    report = build_diagnostic_report(
        fixture,
        fixture_path=fixture_path,
        fixture_sha=fixture_sha,
        reports_by_scorer=reports_by_scorer,
    )
    destination = output_path or default_output_path(fixture_sha)
    write_report(report, destination)
    return report, destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run diagnostic identifier robustness schema matching evaluation.")
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--output", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report, output_path = run_evaluation(args.fixture, args.output)
    print(f"Diagnostic report: {output_path}")
    print(f"Fixture SHA-256  : {report['_meta']['fixture_sha256']}")
    for scorer in SCORERS:
        overall = report["scorers"][scorer]["overall"]
        print(
            f"{scorer}: top1={overall['top1_accuracy']} "
            f"recall@3={overall['recall_at_3']} mrr={overall['mrr']} "
            f"no_target={overall['no_target_accuracy']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
