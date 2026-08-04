from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = PROJECT_ROOT / "src" / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from migration_cutover_findings import (  # noqa: E402
    DEFAULT_GENERATED_VALIDATION_PATH,
    DEFAULT_OUTPUT_PATH,
    DEFAULT_VENDOR_DUPLICATE_PATH,
    DEFAULT_VENDOR_FIELD_MAPPING_PATH,
    DEFAULT_VENDOR_VALIDATION_PATH,
    MigrationFindingError,
    build_migration_cutover_findings,
    load_default_reports,
    project_relative,
    write_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build deterministic module one migration cutover findings.",
    )
    parser.add_argument("--vendor-validation-report", type=Path, default=DEFAULT_VENDOR_VALIDATION_PATH)
    parser.add_argument("--vendor-duplicate-report", type=Path, default=DEFAULT_VENDOR_DUPLICATE_PATH)
    parser.add_argument("--vendor-field-mapping-report", type=Path, default=DEFAULT_VENDOR_FIELD_MAPPING_PATH)
    parser.add_argument("--generated-validation-report", type=Path, default=DEFAULT_GENERATED_VALIDATION_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        inputs = load_default_reports(
            vendor_validation_path=args.vendor_validation_report,
            vendor_duplicate_path=args.vendor_duplicate_report,
            vendor_field_mapping_path=args.vendor_field_mapping_report,
            generated_validation_path=args.generated_validation_report,
        )
        report = build_migration_cutover_findings(inputs)
        report = write_report(report, args.output)
    except MigrationFindingError as error:
        print(f"Migration finding error: {error}", file=sys.stderr)
        return 2

    summary = report["summary"]
    print(f"Findings             : {summary['finding_count']}")
    print(f"Review required      : {summary['review_required_count']}")
    print(f"Gate blockers        : {summary['gate_blocker_count']}")
    print(f"Content SHA          : {report['_run_info']['content_sha256']}")
    print(f"Output               : {project_relative(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
