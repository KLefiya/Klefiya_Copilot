from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.contracts.loader import ContractLoadError, load_migration_contract
from src.core.contracts.validator import (
    ContractValidationError,
    validate_migration_contract,
    write_validation_report,
)


def _project_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a migration package against a declared tabular contract.",
    )
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return exit code 1 when the migration package is invalid.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        contract = load_migration_contract(args.contract, args.data_root)
        report = validate_migration_contract(contract)
        write_validation_report(report, args.output)
    except ContractLoadError as exc:
        print(f"Contract error: {exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except ContractValidationError as exc:
        print(f"Validation error: {exc}", file=sys.stderr)
        return 2

    meta = report["_meta"]
    summary = report["summary"]
    print(f"Contract        : {meta['contract_id']}")
    print(f"Adapter         : {meta['adapter']}")
    print(f"Resources       : {meta['resource_count']}")
    print(f"Rows checked    : {meta['row_count']}")
    print(f"Findings        : {summary['finding_count']}")
    print(f"Valid           : {str(summary['valid']).lower()}")
    print(f"Content         : sha256 {report['_run_info']['content_sha256'][:16]}")
    print(f"Output          : {_project_relative(args.output)}")
    return 1 if args.strict and not summary["valid"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
