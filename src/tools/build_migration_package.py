from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.contracts.loader import ContractLoadError, load_migration_contract
from src.core.package_generation.builder import (
    PackageBuildBlocked,
    PackageGenerationError,
    build_migration_package,
    write_build_report,
)
from src.core.package_generation.decision_loader import DecisionLoadError


def _project_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a target migration package from approved contract mapping decisions.",
    )
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--contract-data-root", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--mapping-report", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--build-report", required=True, type=Path)
    parser.add_argument("--validation-report", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        contract = load_migration_contract(args.contract, args.contract_data_root)
        report = build_migration_package(
            contract,
            args.source,
            args.mapping_report,
            args.decisions,
            args.output_root,
            validation_report_path=args.validation_report,
        )
        write_build_report(report, args.build_report)
    except PackageBuildBlocked as exc:
        print(f"Build blocked: {exc.code}: {exc.message}", file=sys.stderr)
        return 3
    except (ContractLoadError, DecisionLoadError, PackageGenerationError) as exc:
        code = getattr(exc, "code", "input_error")
        message = getattr(exc, "message", str(exc))
        print(f"Input error: {code}: {message}", file=sys.stderr)
        return 2

    meta = report["_meta"]
    summary = report["summary"]
    validation = report["validation"]
    print(f"Contract                   : {meta['contract_id']}")
    print(f"Source rows                : {summary['source_rows']}")
    print(f"Approved mappings          : {summary['approved_mappings']}")
    print(f"Rejected mappings          : {summary['rejected_mappings']}")
    print(f"Resources generated        : {summary['resources_generated']}")
    print(f"Rows generated             : {summary['rows_generated']}")
    print(f"Rejected rows              : {summary['rejected_rows']}")
    print(f"Lineage entries            : {summary['lineage_entries']}")
    print(f"Generated validation valid : {str(validation['valid']).lower()}")
    print(f"Generated findings         : {validation['finding_count']}")
    print(f"Manifest SHA               : {report['manifest']['content_sha256']}")
    print(f"Build report SHA           : {report['_run_info']['content_sha256']}")
    print(f"Output root                : {_project_relative(args.output_root)}")
    if validation["valid"]:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
