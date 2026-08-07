from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path


MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


class BootstrapError(RuntimeError):
    pass


def fetch_model_revision(model_id: str = MODEL_ID) -> str:
    from huggingface_hub import HfApi

    return HfApi(token=False).model_info(model_id, token=False).sha


def download_model_snapshot(model_id: str = MODEL_ID, revision: str = MODEL_REVISION) -> str:
    from huggingface_hub import snapshot_download

    return snapshot_download(repo_id=model_id, revision=revision, token=False)


def write_local_main_ref(snapshot_path: str, revision: str = MODEL_REVISION) -> None:
    resolved = Path(snapshot_path)
    refs_dir = resolved.parent.parent / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / "main").write_text(revision, encoding="utf-8")


def verify_local_load(model_id: str = MODEL_ID, revision: str = MODEL_REVISION) -> None:
    from sentence_transformers import SentenceTransformer

    SentenceTransformer(model_id, local_files_only=True)


def bootstrap_model(
    *,
    model_id: str = MODEL_ID,
    expected_revision: str = MODEL_REVISION,
    revision_reader: Callable[[str], str] = fetch_model_revision,
    downloader: Callable[[str, str], str] = download_model_snapshot,
    local_ref_writer: Callable[[str, str], None] = write_local_main_ref,
    local_loader: Callable[[str, str], None] = verify_local_load,
) -> dict[str, str]:
    actual_revision = revision_reader(model_id)
    if actual_revision != expected_revision:
        raise BootstrapError(
            f"Unexpected revision for {model_id}: expected {expected_revision}, got {actual_revision}"
        )

    snapshot_path = downloader(model_id, expected_revision)
    local_ref_writer(snapshot_path, expected_revision)
    try:
        local_loader(model_id, expected_revision)
    except Exception as exc:  # pragma: no cover - exact dependency exception is platform-specific.
        raise BootstrapError(f"Local-only SentenceTransformer load failed for {model_id}.") from exc

    return {
        "model_id": model_id,
        "revision": expected_revision,
        "snapshot_path": snapshot_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the pinned CI embedding model.")
    parser.add_argument("--model-id", default=MODEL_ID)
    parser.add_argument("--revision", default=MODEL_REVISION)
    args = parser.parse_args(argv)

    try:
        result = bootstrap_model(model_id=args.model_id, expected_revision=args.revision)
    except BootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Model ID: {result['model_id']}")
    print(f"Revision: {result['revision']}")
    print(f"Snapshot: {result['snapshot_path']}")
    print("Local-only load: valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
