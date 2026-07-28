from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.mapping.evaluator import (
    MappingEvaluationError,
    evaluate_mapping_report,
    write_evaluation_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate a contract mapping report against synthetic ground truth.")
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = evaluate_mapping_report(args.mapping, args.ground_truth)
        write_evaluation_report(report, args.output)
    except (MappingEvaluationError, ValueError, FileNotFoundError, KeyError) as exc:
        code = getattr(exc, "code", "evaluation_input_error")
        message = getattr(exc, "message", str(exc))
        print(f"Evaluation error: {code}: {message}", file=sys.stderr)
        return 2
    summary = report["summary"]
    print(f"Evaluated fields          : {summary['evaluated_fields']}")
    print(f"Top1 accuracy            : {summary['top1_accuracy']:.4f}")
    print(f"Top3 recall              : {summary['top3_recall']:.4f}")
    print(f"High confidence precision: {summary['high_confidence_precision']:.4f}")
    print(f"No target accuracy       : {summary['no_target_accuracy']:.4f}")
    print(f"Content SHA              : {report['_run_info']['content_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
