from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.contracts.loader import ContractLoadError, load_migration_contract
from src.core.mapping.engine import suggest_contract_mappings, write_mapping_report
from src.core.mapping.profiler import SourceProfileError
from src.core.mapping.scorer import DEFAULT_MODEL_NAME, MappingModelError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Suggest field mappings from a source CSV to a migration contract.")
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL_NAME)
    return parser


def _project_relative(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        contract = load_migration_contract(args.contract, args.data_root)
        report = suggest_contract_mappings(contract, args.source, model_name=args.model)
        write_mapping_report(report, args.output)
    except (ContractLoadError, SourceProfileError, MappingModelError, ValueError) as exc:
        code = getattr(exc, "code", "mapping_input_error")
        message = getattr(exc, "message", str(exc))
        print(f"Mapping error: {code}: {message}", file=sys.stderr)
        return 2
    meta = report["_meta"]
    summary = report["summary"]
    print(f"Contract            : {meta['contract_id']}")
    print(f"Source rows         : {meta['source_row_count']}")
    print(f"Source fields       : {meta['source_field_count']}")
    print(f"Target fields       : {meta['target_field_count']}")
    print(f"Suggested           : {summary['suggested']}")
    print(f"Needs review        : {summary['needs_review']}")
    print(f"No confident target : {summary['no_confident_target']}")
    print(f"Alias based         : {summary['alias_based']}")
    print(f"Semantic based      : {summary['semantic_based']}")
    print(f"Content SHA         : {report['_run_info']['content_sha256']}")
    print(f"Output              : {_project_relative(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
