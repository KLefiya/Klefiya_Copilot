from __future__ import annotations

import os
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import bootstrap_ci_embedding_model as bootstrap


class CIModelBootstrapTests(unittest.TestCase):
    def test_correct_revision_downloads_and_loads_local_only(self) -> None:
        calls: list[tuple[str, str, str | None]] = []

        def downloader(model_id: str, revision: str) -> str:
            calls.append(("download", model_id, revision))
            return "/tmp/model"

        def loader(model_id: str, revision: str) -> None:
            calls.append(("load", model_id, revision))

        def ref_writer(snapshot_path: str, revision: str) -> None:
            calls.append(("ref", snapshot_path, revision))

        result = bootstrap.bootstrap_model(
            revision_reader=lambda model_id: bootstrap.MODEL_REVISION,
            downloader=downloader,
            local_ref_writer=ref_writer,
            local_loader=loader,
        )

        self.assertEqual(result["model_id"], bootstrap.MODEL_ID)
        self.assertEqual(result["revision"], bootstrap.MODEL_REVISION)
        self.assertEqual(calls, [
            ("download", bootstrap.MODEL_ID, bootstrap.MODEL_REVISION),
            ("ref", "/tmp/model", bootstrap.MODEL_REVISION),
            ("load", bootstrap.MODEL_ID, bootstrap.MODEL_REVISION),
        ])

        model_info = mock.Mock(return_value=SimpleNamespace(sha=bootstrap.MODEL_REVISION))
        snapshot_download = mock.Mock(return_value="/tmp/model")
        hf_api = mock.Mock(return_value=SimpleNamespace(model_info=model_info))
        fake_hub = SimpleNamespace(HfApi=hf_api, snapshot_download=snapshot_download)
        with (
            mock.patch.dict(os.environ, {"HF_TOKEN": "definitely-not-a-real-token"}, clear=False),
            mock.patch.dict(sys.modules, {"huggingface_hub": fake_hub}),
        ):
            self.assertEqual(bootstrap.fetch_model_revision(), bootstrap.MODEL_REVISION)
            self.assertEqual(bootstrap.download_model_snapshot(), "/tmp/model")
        hf_api.assert_called_once_with(token=False)
        model_info.assert_called_once_with(bootstrap.MODEL_ID, token=False)
        snapshot_download.assert_called_once_with(
            repo_id=bootstrap.MODEL_ID,
            revision=bootstrap.MODEL_REVISION,
            token=False,
        )

    def test_revision_mismatch_fails_before_download(self) -> None:
        downloader = mock.Mock(return_value="/tmp/model")
        with self.assertRaisesRegex(bootstrap.BootstrapError, "Unexpected revision"):
            bootstrap.bootstrap_model(
                revision_reader=lambda model_id: "0" * 40,
                downloader=downloader,
                local_ref_writer=lambda snapshot_path, revision: None,
                local_loader=lambda model_id, revision: None,
            )
        downloader.assert_not_called()

    def test_local_only_load_failure_fails(self) -> None:
        def loader(model_id: str, revision: str) -> None:
            raise RuntimeError("missing cache")

        with self.assertRaisesRegex(bootstrap.BootstrapError, "Local-only"):
            bootstrap.bootstrap_model(
                revision_reader=lambda model_id: bootstrap.MODEL_REVISION,
                downloader=lambda model_id, revision: "/tmp/model",
                local_ref_writer=lambda snapshot_path, revision: None,
                local_loader=loader,
            )

    def test_api_token_environment_is_ignored_by_anonymous_calls(self) -> None:
        for key in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN"):
            with self.subTest(key=key):
                with mock.patch.dict(os.environ, {key: "definitely-not-a-real-token"}, clear=False):
                    result = bootstrap.bootstrap_model(
                        revision_reader=lambda model_id: bootstrap.MODEL_REVISION,
                        downloader=lambda model_id, revision: "/tmp/model",
                        local_ref_writer=lambda snapshot_path, revision: None,
                        local_loader=lambda model_id, revision: None,
                    )
                self.assertEqual(result["revision"], bootstrap.MODEL_REVISION)

    def test_does_not_read_process_api_token_state(self) -> None:
        with mock.patch.dict(os.environ, {"HF_TOKEN": "definitely-not-a-real-token"}, clear=False):
            result = bootstrap.bootstrap_model(
                revision_reader=lambda model_id: bootstrap.MODEL_REVISION,
                downloader=lambda model_id, revision: "/tmp/model",
                local_ref_writer=lambda snapshot_path, revision: None,
                local_loader=lambda model_id, revision: None,
            )
        self.assertEqual(result["model_id"], bootstrap.MODEL_ID)

    def test_write_local_main_ref_creates_cache_ref(self) -> None:
        with self.subTest("cache layout"):
            from tempfile import TemporaryDirectory
            from pathlib import Path

            with TemporaryDirectory() as tmp:
                snapshot = Path(tmp) / "hub" / "models--x--y" / "snapshots" / bootstrap.MODEL_REVISION
                snapshot.mkdir(parents=True)
                bootstrap.write_local_main_ref(str(snapshot), bootstrap.MODEL_REVISION)
                self.assertEqual(
                    (Path(tmp) / "hub" / "models--x--y" / "refs" / "main").read_text(encoding="utf-8"),
                    bootstrap.MODEL_REVISION,
                )


if __name__ == "__main__":
    unittest.main()
