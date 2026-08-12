from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.contracts.loader import ContractLoadError
from src.core.mapping.benchmark import (
    SchemaMatchingBenchmarkError,
    benchmark_run_specs,
    evaluate_benchmark,
    generate_candidate_reports,
    load_benchmark,
    write_benchmark_report,
)
from src.core.mapping.profiler import SourceProfileError
from src.core.mapping.scorer import DEFAULT_MODEL_NAME, MappingModelError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate the schema matching benchmark with the production scorer.")
    parser.add_argument("--benchmark", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL_NAME)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    args = build_parser().parse_args(argv)
    try:
        benchmark = load_benchmark(args.benchmark)
        run_specs = benchmark_run_specs(benchmark)
        candidate_reports = generate_candidate_reports(run_specs, model_name=args.model)
        report = evaluate_benchmark(benchmark, candidate_reports)
        output = write_benchmark_report(report, args.output)
    except (
        ContractLoadError,
        MappingModelError,
        SchemaMatchingBenchmarkError,
        SourceProfileError,
        ValueError,
    ) as exc:
        code = getattr(exc, "code", "schema_matching_benchmark_error")
        message = getattr(exc, "message", str(exc))
        print(f"Schema matching benchmark error: {code}: {message}", file=sys.stderr)
        return 2
    overall = report["overall"]
    print(f"Scenario count                  : {overall['scenario_count']}")
    print(f"Case count                      : {overall['case_count']}")
    print(f"Expected target links           : {overall['expected_target_link_count']}")
    print(f"Single-target Top-1 accuracy    : {_format_metric(overall['single_target_top1_accuracy'])}")
    print(f"Target-link recall@1            : {_format_metric(overall['target_link_recall_at_1'])}")
    print(f"Target-link recall@3            : {_format_metric(overall['target_link_recall_at_3'])}")
    print(f"Target-link MRR                 : {_format_metric(overall['target_link_mrr'])}")
    print(f"No-target accuracy              : {_format_metric(overall['no_target_accuracy'])}")
    print(f"Multi-target full coverage@3    : {_format_metric(overall['multi_target_full_coverage_at_3'])}")
    print(f"Content SHA                     : {report['_run_info']['content_sha256']}")
    print(f"Output                          : {output}")
    return 0


def _format_metric(value: float | None) -> str:
    return "null" if value is None else f"{value:.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
