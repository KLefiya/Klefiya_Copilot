from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PROJECT_ROOT = REPO_ROOT
FIXTURE_DIR = PROJECT_ROOT / "data" / "benchmarks" / "external" / "companies_house_customer_v1"
SOURCE_PATH = FIXTURE_DIR / "source_companies_house_customer.csv"
GROUND_TRUTH_PATH = FIXTURE_DIR / "ground_truth.json"
PROVENANCE_PATH = FIXTURE_DIR / "source_provenance.json"
PROTOCOL_PATH = FIXTURE_DIR / "protocol_lock.json"
FIXTURE_README_PATH = FIXTURE_DIR / "README.md"
PREPARATION_SCRIPT_PATH = PROJECT_ROOT / "scripts" / "prepare_companies_house_external_fixture.py"
TARGET_CONTRACT_PATH = PROJECT_ROOT / "contracts" / "generic_customer" / "datapackage.yaml"
OUTPUT_PATH = FIXTURE_DIR / "first_evaluation_baseline_v4_v5.json"
RUNNER_PATH = Path(__file__).resolve()

REGISTRATION_COMMIT = "66ae6fe8f55bd9efc782820622eb27c0c6e1f4ef"
ALGORITHM_BASELINE_COMMIT = "23add9d90fe93c590f32e946f471fb929cb88ac3"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
SCENARIO_ID = "companies_house_customer_external_v1"
EXPECTED_TARGET_CONTRACT_GIT_BLOB_SHA = (
    "8fe32d08f23a2c97dedea8d43d37d96925003766acbd4f69326b7646b90da792"
)

EXPECTED_HASHES = {
    SOURCE_PATH: "e4b3f0b7a3ee4fe497fc6e8de2ad1fa5fc9d46209022c8aa36300800eabe0be4",
    GROUND_TRUTH_PATH: "fcb01c248ff403d2d13d6d31a551a80055c329d692b4698beb2028b30d705f05",
    PROVENANCE_PATH: "365e24eca399a26ae8dbad85c73db0cb51137dbef11d047e44b977e1da3444be",
    PROTOCOL_PATH: "99dca725014b056bd47a879dc9461009935a2416b5ab7100859dfca3aa65e723",
    FIXTURE_README_PATH: "2ea5e5391867a0f31db16a4c6481ba58c474f32badac918962b6730290763789",
    PREPARATION_SCRIPT_PATH: "aad5978d7387a89ce53b2108bcec97566603241af8e563004f6ef946ce68e79c",
}

EXPECTED_SCORERS = ("baseline", "precision_tiered_v4", "precision_tiered_v5")
EXPECTED_COLUMNS = (
    "CompanyName",
    "CompanyNumber",
    "RegAddress.Country",
    "CompanyCategory",
    "CompanyStatus",
    "IncorporationDate",
    "Accounts.AccountCategory",
    "Accounts.NextDueDate",
    "SICCode.SicText_1",
    "ConfStmtNextDueDate",
    "Mortgages.NumMortCharges",
    "URI",
)
EXPECTED_COUNTS = {
    "single_target_cases": 3,
    "multi_target_cases": 0,
    "no_target_cases": 9,
    "target_links": 3,
}
EXPECTED_TARGET_ALLOWLIST = (
    "customer.customer_id",
    "customer.customer_name",
    "customer.country",
    "customer.email",
    "customer.phone",
    "customer.tax_number",
    "customer.payment_terms",
    "customer_bank.bank_id",
    "customer_bank.customer_id",
    "customer_bank.iban",
    "customer_bank.currency",
)


class ExternalEvaluationError(RuntimeError):
    pass


class CountingEmbeddingBackend:
    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self.encode_call_count = 0
        self.encoded_sentence_count = 0

    def encode(self, sentences: Any, **kwargs: Any) -> Any:
        self.encode_call_count += 1
        if isinstance(sentences, str):
            self.encoded_sentence_count += 1
            payload = sentences
        else:
            payload = list(sentences)
            self.encoded_sentence_count += len(payload)
        return self.backend.encode(payload, **kwargs)


def raw_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise ExternalEvaluationError(f"refusing to overwrite existing artifact: {project_relative(path)}")
    temp_path = path.with_name(f".{path.name}.tmp")
    if temp_path.exists():
        raise ExternalEvaluationError(f"refusing to reuse existing temp artifact: {project_relative(temp_path)}")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        validate_artifact_schema(payload, artifact_text=text)
        temp_path.write_text(text + "\n", encoding="utf-8", newline="\n")
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def git_blob_content_sha(path: Path) -> str:
    rel = project_relative(path)
    result = subprocess.run(
        ["git", "show", f"HEAD:{rel}"],
        cwd=PROJECT_ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return sha256(result.stdout).hexdigest()


def validate_frozen_inputs(*, output_path: Path, include_ground_truth_counts: bool) -> dict[str, Any]:
    if output_path.exists():
        raise ExternalEvaluationError(f"output already exists: {project_relative(output_path)}")
    temp_path = output_path.with_name(f".{output_path.name}.tmp")
    if temp_path.exists():
        raise ExternalEvaluationError(f"temp output already exists: {project_relative(temp_path)}")
    for path, expected in EXPECTED_HASHES.items():
        actual = raw_sha256(path)
        if actual != expected:
            raise ExternalEvaluationError(
                f"frozen hash mismatch for {project_relative(path)}: expected {expected}, got {actual}"
            )
    protocol = read_json(PROTOCOL_PATH)
    if protocol["first_evaluation_status"] != "not_run":
        raise ExternalEvaluationError("protocol first_evaluation_status is not not_run")
    if protocol["evaluation_artifact"] is not None or protocol["evaluation_output"] is not None:
        raise ExternalEvaluationError("protocol evaluation artifact/output must remain null")
    if tuple(protocol["planned_scorers"]) != EXPECTED_SCORERS:
        raise ExternalEvaluationError("protocol planned scorers changed")
    if protocol["header_normalization_rule"] != "strip ASCII space (0x20) from header boundaries only":
        raise ExternalEvaluationError("unexpected header normalization rule")
    if protocol["source_values_normalized"] is not False:
        raise ExternalEvaluationError("source values must not be normalized")
    if EXPECTED_TARGET_CONTRACT_GIT_BLOB_SHA != git_blob_content_sha(TARGET_CONTRACT_PATH):
        raise ExternalEvaluationError("target contract Git blob content SHA mismatch")
    for scorer_id, item in protocol["scorer_source_file_hashes"].items():
        path = PROJECT_ROOT / item["path"]
        if raw_sha256(path) != item["raw_sha256"]:
            raise ExternalEvaluationError(f"scorer source hash mismatch for {scorer_id}")
    with SOURCE_PATH.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        rows = list(reader)
    if tuple(header) != EXPECTED_COLUMNS:
        raise ExternalEvaluationError("source fixture header changed")
    if len(rows) != 128 or any(len(row) != len(EXPECTED_COLUMNS) for row in rows):
        raise ExternalEvaluationError("source fixture must be 128 x 12")
    if include_ground_truth_counts:
        ground_truth = read_json(GROUND_TRUTH_PATH)
        if ground_truth["counts"] != EXPECTED_COUNTS:
            raise ExternalEvaluationError("ground truth counts changed")
        if ground_truth["source_fields"] != list(EXPECTED_COLUMNS):
            raise ExternalEvaluationError("ground truth source field order changed")
    return protocol


def validate_canonical_import_path() -> None:
    from src.core.mapping.benchmark import generate_candidate_reports

    if not callable(generate_candidate_reports):
        raise ExternalEvaluationError("canonical generate_candidate_reports import is not callable")


def _metric(numerator: int | float, denominator: int) -> dict[str, Any]:
    value = None if denominator == 0 else round(float(numerator) / denominator, 4)
    body: dict[str, Any] = {
        "numerator": round(float(numerator), 4) if isinstance(numerator, float) else numerator,
        "denominator": denominator,
        "value": value,
    }
    if denominator == 0:
        body["status"] = "not_applicable"
    return body


def _expected_rank(expected_targets: list[str], top_candidates: list[dict[str, Any]]) -> int | None:
    for candidate in top_candidates:
        if candidate["target"] in expected_targets:
            return int(candidate["rank"])
    return None


def compute_metrics(
    ground_truth: dict[str, Any],
    predictions: list[dict[str, Any]],
) -> dict[str, Any]:
    cases_by_field = {case["source_field"]: case for case in ground_truth["cases"]}
    predictions_by_field = {item["source_field"]: item for item in predictions}
    if set(cases_by_field) != set(predictions_by_field):
        raise ExternalEvaluationError("prediction fields do not match ground truth fields")
    counts = dict(EXPECTED_COUNTS)
    single_top1 = 0
    links_at_1 = 0
    links_at_3 = 0
    mrr_points = 0.0
    no_target_correct = 0
    status_counts: Counter[str] = Counter()
    recommendation_counts: Counter[str] = Counter()
    coverage_counts: Counter[str] = Counter()
    for source_field in ground_truth["source_fields"]:
        case = cases_by_field[source_field]
        prediction = predictions_by_field[source_field]
        expected = list(case["expected_targets"])
        top3 = list(prediction["top_candidates"])
        recommendation = prediction["recommendation"]
        status_counts[str(prediction["status"])] += 1
        recommendation_counts["recommended"] += int(recommendation is not None)
        recommendation_counts["no_recommendation"] += int(recommendation is None)
        coverage_counts["has_top3"] += int(len(top3) == 3)
        if case["case_type"] == "single_target":
            rank = _expected_rank(expected, top3)
            single_top1 += int(rank == 1)
            links_at_1 += int(rank == 1)
            links_at_3 += int(rank is not None and rank <= 3)
            mrr_points += (1.0 / rank) if rank else 0.0
        elif case["case_type"] == "no_target":
            no_target_correct += int(recommendation is None)
    return {
        "single_target_top1_accuracy": _metric(single_top1, counts["single_target_cases"]),
        "target_link_recall_at_1": _metric(links_at_1, counts["target_links"]),
        "target_link_recall_at_3": _metric(links_at_3, counts["target_links"]),
        "mean_reciprocal_rank": _metric(round(mrr_points, 4), counts["target_links"]),
        "no_target_accuracy": _metric(no_target_correct, counts["no_target_cases"]),
        "multi_target_full_recall_at_3": _metric(0, counts["multi_target_cases"]),
        "coverage": dict(sorted(coverage_counts.items())),
        "recommendation_counts": dict(sorted(recommendation_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
    }


def sanitize_report(report: dict[str, Any], target_allowlist: set[str]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for mapping in sorted(report["mappings"], key=lambda item: item["source_field"]):
        top_candidates = []
        for candidate in list(mapping.get("top_candidates", []))[:3]:
            target = str(candidate["target"])
            if target not in target_allowlist:
                raise ExternalEvaluationError(f"candidate target outside allowlist: {target}")
            top_candidates.append(
                {
                    "rank": int(candidate["rank"]),
                    "score": round(float(candidate["score"]), 4),
                    "target": target,
                }
            )
        if len(top_candidates) != 3:
            raise ExternalEvaluationError(f"missing top-3 candidates for {mapping['source_field']}")
        recommendation = mapping.get("recommendation")
        if recommendation is not None and str(recommendation) not in target_allowlist:
            raise ExternalEvaluationError(f"recommendation outside allowlist: {recommendation}")
        sanitized.append(
            {
                "confidence": round(float(mapping.get("confidence") or 0.0), 4),
                "recommendation": None if recommendation is None else str(recommendation),
                "source_field": str(mapping["source_field"]),
                "status": str(mapping["status"]),
                "top_candidates": top_candidates,
            }
        )
    if [item["source_field"] for item in sanitized] != sorted(EXPECTED_COLUMNS):
        raise ExternalEvaluationError("sanitized predictions do not cover the expected source fields")
    return sanitized


def load_scorer_metadata() -> dict[str, dict[str, Any]]:
    from src.core.mapping.scorer_v4 import metadata as v4_metadata
    from src.core.mapping.scorer_v5 import metadata as v5_metadata

    v4 = v4_metadata()
    v5 = v5_metadata()
    return {
        "baseline": {
            "feature_version": None,
            "parent_scorer": None,
            "scorer_id": "baseline",
        },
        "precision_tiered_v4": {
            "feature_version": v4["feature_version"],
            "parent_scorer": v4["parent_scorer"],
            "scorer_id": v4["scorer_id"],
        },
        "precision_tiered_v5": {
            "feature_version": v5["feature_version"],
            "parent_scorer": v5["parent_scorer"],
            "scorer_id": v5["scorer_id"],
        },
    }


def generate_all_predictions() -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], dict[str, Any]]:
    from src.core.mapping.benchmark import generate_candidate_reports
    from src.core.mapping.scorer import load_embedding_backend

    if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("TRANSFORMERS_OFFLINE") != "1":
        raise ExternalEvaluationError("HF_HUB_OFFLINE and TRANSFORMERS_OFFLINE must both be set to 1")

    base_backend = load_embedding_backend(MODEL_NAME)
    backend = CountingEmbeddingBackend(base_backend)
    run_specs = [
        {
            "contract_path": "contracts/generic_customer/datapackage.yaml",
            "data_root_path": "data/examples/generic_customer",
            "scenario_id": SCENARIO_ID,
            "source_path": "data/benchmarks/external/companies_house_customer_v1/source_companies_house_customer.csv",
        }
    ]
    predictions: dict[str, list[dict[str, Any]]] = {}
    runtime_by_scorer: dict[str, Any] = {}
    target_allowlist = set(EXPECTED_TARGET_ALLOWLIST)
    for scorer_id in EXPECTED_SCORERS:
        before_calls = backend.encode_call_count
        before_sentences = backend.encoded_sentence_count
        reports = generate_candidate_reports(
            run_specs,
            model_name=MODEL_NAME,
            embedding_backend=backend,
            scorer_variant=scorer_id,
        )
        report = reports[SCENARIO_ID]
        predictions[scorer_id] = sanitize_report(report, target_allowlist)
        runtime_by_scorer[scorer_id] = {
            "encode_call_count": backend.encode_call_count - before_calls,
            "encoded_sentence_count": backend.encoded_sentence_count - before_sentences,
        }
    model_runtime = {
        "encoded_sentence_count": backend.encoded_sentence_count,
        "encode_call_count": backend.encode_call_count,
        "hf_hub_offline_enabled": True,
        "local_files_only": True,
        "model_load_count": 1,
        "model_name": MODEL_NAME,
        "transformers_offline_enabled": True,
    }
    return predictions, model_runtime, runtime_by_scorer


def build_artifact(
    protocol: dict[str, Any],
    predictions_by_scorer: dict[str, list[dict[str, Any]]],
    model_runtime: dict[str, Any],
    runtime_by_scorer: dict[str, Any],
) -> dict[str, Any]:
    ground_truth = read_json(GROUND_TRUTH_PATH)
    scorer_metadata = load_scorer_metadata()
    scorers: dict[str, Any] = {}
    for scorer_id in EXPECTED_SCORERS:
        predictions = predictions_by_scorer[scorer_id]
        scorers[scorer_id] = {
            "metrics": compute_metrics(ground_truth, predictions),
            "model_runtime": runtime_by_scorer[scorer_id],
            "predictions": predictions,
            "scorer_metadata": scorer_metadata[scorer_id],
            "source_fields_evaluated": len(predictions),
        }
    return {
        "algorithm_baseline_commit": ALGORITHM_BASELINE_COMMIT,
        "artifact_type": "companies_house_external_benchmark_first_evaluation",
        "artifact_version": "1.0",
        "case_counts": EXPECTED_COUNTS,
        "evaluation_ordinal": 1,
        "evaluation_runner": {
            "path": project_relative(RUNNER_PATH),
            "raw_sha256": raw_sha256(RUNNER_PATH),
        },
        "evaluation_status": "completed",
        "first_evaluation_contract": {
            "ground_truth_loaded_after_all_scorer_predictions": True,
            "protocol_file_left_unmodified": True,
            "result_does_not_update_algorithm_fixture_ground_truth_or_protocol": True,
        },
        "execution_history": {
            "pre_prediction_infrastructure_aborts": [
                {
                    "artifact_created": False,
                    "candidate_generation_started": False,
                    "error_code": "module_not_found_src",
                    "model_loaded": False,
                    "predictions_computed": False,
                    "scorer_execution_started": False,
                    "stage": "canonical_benchmark_import",
                }
            ],
            "substantive_evaluation_ordinal": 1,
        },
        "frozen_inputs": {
            "fixture_readme": {
                "path": project_relative(FIXTURE_README_PATH),
                "raw_sha256": EXPECTED_HASHES[FIXTURE_README_PATH],
            },
            "ground_truth": {
                "path": project_relative(GROUND_TRUTH_PATH),
                "raw_sha256": EXPECTED_HASHES[GROUND_TRUTH_PATH],
            },
            "preparation_script": {
                "path": project_relative(PREPARATION_SCRIPT_PATH),
                "raw_sha256": EXPECTED_HASHES[PREPARATION_SCRIPT_PATH],
            },
            "protocol": {
                "path": project_relative(PROTOCOL_PATH),
                "raw_sha256": EXPECTED_HASHES[PROTOCOL_PATH],
            },
            "source_fixture": {
                "path": project_relative(SOURCE_PATH),
                "raw_sha256": EXPECTED_HASHES[SOURCE_PATH],
            },
            "source_provenance": {
                "path": project_relative(PROVENANCE_PATH),
                "raw_sha256": EXPECTED_HASHES[PROVENANCE_PATH],
            },
            "target_contract": {
                "path": project_relative(TARGET_CONTRACT_PATH),
                "target_contract_git_blob_content_sha256": EXPECTED_TARGET_CONTRACT_GIT_BLOB_SHA,
            },
        },
        "header_normalization": {
            "frozen_before_first_evaluation": protocol[
                "header_normalization_frozen_before_first_evaluation"
            ],
            "reads_target_aliases_ground_truth_or_predictions": protocol[
                "header_normalization_reads_target_aliases_ground_truth_or_predictions"
            ],
            "rule": protocol["header_normalization_rule"],
            "source_values_normalized": False,
        },
        "model_execution": model_runtime,
        "planned_scorers": list(EXPECTED_SCORERS),
        "privacy": {
            "contains_company_names": False,
            "contains_company_numbers": False,
            "contains_local_absolute_paths": False,
            "contains_row_source_values": False,
            "prediction_payload_fields": [
                "source_field",
                "status",
                "recommendation",
                "confidence",
                "top_candidates.rank",
                "top_candidates.target",
                "top_candidates.score",
            ],
        },
        "registration_commit": REGISTRATION_COMMIT,
        "scenario_id": SCENARIO_ID,
        "scorer_source_file_hashes": protocol["scorer_source_file_hashes"],
        "scorers": scorers,
    }


def source_values_to_exclude() -> set[str]:
    values: set[str] = set()
    with SOURCE_PATH.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            for value in row.values():
                if value and len(value) >= 8:
                    values.add(value)
    return values


def validate_artifact_schema(payload: dict[str, Any], *, artifact_text: str | None = None) -> None:
    if payload["evaluation_status"] != "completed":
        raise ExternalEvaluationError("artifact must be completed")
    if payload["evaluation_ordinal"] != 1:
        raise ExternalEvaluationError("artifact must be first evaluation ordinal")
    if payload["registration_commit"] != REGISTRATION_COMMIT:
        raise ExternalEvaluationError("registration commit mismatch")
    if payload["algorithm_baseline_commit"] != ALGORITHM_BASELINE_COMMIT:
        raise ExternalEvaluationError("algorithm baseline commit mismatch")
    if set(payload["scorers"]) != set(EXPECTED_SCORERS):
        raise ExternalEvaluationError("artifact scorer set mismatch")
    for scorer_id, result in payload["scorers"].items():
        predictions = result["predictions"]
        if [item["source_field"] for item in predictions] != sorted(EXPECTED_COLUMNS):
            raise ExternalEvaluationError(f"prediction fields changed for {scorer_id}")
        for prediction in predictions:
            forbidden_prediction_keys = {"expected_targets", "ground_truth", "case_type"}
            if forbidden_prediction_keys.intersection(prediction):
                raise ExternalEvaluationError("prediction payload contains ground truth fields")
            for candidate in prediction["top_candidates"]:
                if set(candidate) != {"rank", "score", "target"}:
                    raise ExternalEvaluationError("candidate payload is not privacy-minimal")
    text = artifact_text if artifact_text is not None else json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if re.search(r"[A-Za-z]:\\\\", text):
        raise ExternalEvaluationError("artifact contains a Windows absolute path")
    if re.search(r"(?<!https:)//", text):
        raise ExternalEvaluationError("artifact contains a Unix-style absolute path")
    for value in source_values_to_exclude():
        if value in text:
            raise ExternalEvaluationError("artifact contains row source value")


def run_validate_only(output_path: Path) -> None:
    validate_frozen_inputs(output_path=output_path, include_ground_truth_counts=True)
    validate_canonical_import_path()
    print(
        json.dumps(
            {
                "formal_status": "validated_imports_without_model_or_predictions",
                "output_path_absent": True,
                "planned_scorers": list(EXPECTED_SCORERS),
                "scenario_id": SCENARIO_ID,
            },
            indent=2,
            sort_keys=True,
        )
    )


def run_evaluation(output_path: Path) -> None:
    protocol = validate_frozen_inputs(output_path=output_path, include_ground_truth_counts=False)
    predictions, model_runtime, runtime_by_scorer = generate_all_predictions()
    artifact = build_artifact(protocol, predictions, model_runtime, runtime_by_scorer)
    write_json_atomic(output_path, artifact)
    print(
        json.dumps(
            {
                "evaluation_status": "completed",
                "output_path": project_relative(output_path),
                "scorers": {
                    scorer_id: artifact["scorers"][scorer_id]["metrics"]
                    for scorer_id in EXPECTED_SCORERS
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--output", default=project_relative(OUTPUT_PATH))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    output_path = (PROJECT_ROOT / args.output).resolve()
    try:
        output_path.relative_to(PROJECT_ROOT)
    except ValueError:
        raise ExternalEvaluationError("output must stay inside the project")
    if output_path != OUTPUT_PATH:
        raise ExternalEvaluationError(f"output must be {project_relative(OUTPUT_PATH)}")
    if args.validate_only:
        run_validate_only(output_path)
    else:
        run_evaluation(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
