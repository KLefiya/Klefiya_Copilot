from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from pydantic import BaseModel, ValidationError

from src.tools import gap_analysis as ga


SYSTEM = "system prompt"
USER = "user prompt"


def extraction_payload() -> dict:
    return {
        "requirements": [
            {
                "requirement_description": "Approve purchase orders above a threshold.",
                "domain": "P2P",
                "source_quote": "We need approval above a threshold.",
            }
        ]
    }


def judgement_payload(**overrides) -> dict:
    payload = {
        "category": "Configuration",
        "confidence": 0.82,
        "evidence_entry_ids": ["KB-XC-003"],
        "rationale": "KB-XC-003 says thresholds are configuration.",
    }
    payload.update(overrides)
    return payload


def openai_response(payload: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content=json.dumps(payload))
            )
        ]
    )


def anthropic_module(fake_client) -> types.SimpleNamespace:
    return types.SimpleNamespace(Anthropic=mock.Mock(return_value=fake_client))


def openai_module(fake_client) -> types.SimpleNamespace:
    return types.SimpleNamespace(OpenAI=mock.Mock(return_value=fake_client))


def fake_key(provider: str) -> str:
    return "test-" + provider + "-key"


class EnvCase(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.temp_dirs: list[tempfile.TemporaryDirectory] = []

    def tearDown(self) -> None:
        for temp_dir in reversed(self.temp_dirs):
            temp_dir.cleanup()
        self.env.stop()

    def make_cache_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.temp_dirs.append(temp_dir)
        return Path(temp_dir.name)


class ProviderSelectionTests(EnvCase):
    def test_default_provider_is_deepseek(self) -> None:
        llm = ga.CachedLLM(cache_dir=self.make_cache_dir())
        self.assertEqual(llm.provider, "deepseek")
        self.assertEqual(llm.model, "deepseek-v4-pro")
        self.assertEqual(llm.model, ga.DEFAULT_MODELS["deepseek"])
        self.assertEqual(llm.thinking, "enabled")
        self.assertEqual(llm.reasoning_effort, "high")

    def test_environment_selects_provider_and_model(self) -> None:
        for provider in ("anthropic", "openai", "deepseek"):
            with self.subTest(provider=provider):
                with mock.patch.dict(
                    os.environ,
                    {
                        "CARVEOPS_LLM_PROVIDER": provider,
                        "CARVEOPS_LLM_MODEL": f"{provider}-model",
                    },
                    clear=True,
                ):
                    llm = ga.CachedLLM(cache_dir=self.make_cache_dir())
                    self.assertEqual(llm.provider, provider)
                    self.assertEqual(llm.model, f"{provider}-model")

    def test_explicit_provider_and_model_override_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CARVEOPS_LLM_PROVIDER": "anthropic", "CARVEOPS_LLM_MODEL": "env-model"},
            clear=True,
        ):
            llm = ga.CachedLLM(
                provider="openai",
                model="cli-model",
                cache_dir=self.make_cache_dir(),
            )
        self.assertEqual(llm.provider, "openai")
        self.assertEqual(llm.model, "cli-model")

    def test_invalid_provider_fails_with_allowed_values(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "anthropic.*deepseek.*openai"):
            ga.CachedLLM(provider="unknown", cache_dir=self.make_cache_dir())

    def test_invalid_deepseek_thinking_fails(self) -> None:
        with mock.patch.dict(
            os.environ, {"CARVEOPS_DEEPSEEK_THINKING": "auto"}, clear=True
        ):
            with self.assertRaisesRegex(RuntimeError, "CARVEOPS_DEEPSEEK_THINKING"):
                ga.CachedLLM(provider="deepseek", cache_dir=self.make_cache_dir())

    def test_invalid_deepseek_reasoning_effort_fails(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CARVEOPS_DEEPSEEK_REASONING_EFFORT": "medium"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "CARVEOPS_DEEPSEEK_REASONING_EFFORT"):
                ga.CachedLLM(provider="deepseek", cache_dir=self.make_cache_dir())


class ExtractionPromptTests(unittest.TestCase):
    def test_extraction_prompt_defines_coherent_requirement_granularity(self) -> None:
        prompt = ga.EXTRACTION_SYSTEM
        self.assertIn("coherent business objective", prompt)
        self.assertIn("approval thresholds or approver tiers", prompt)
        self.assertIn("steps of one standard business process", prompt)
        self.assertIn("names several fields", prompt)
        self.assertIn("Do not merge genuinely independent requirements", prompt)
        self.assertNotRegex(prompt, r"REQ-[A-Z0-9]+-\d+")

        schema = ga.ExtractionResult.model_json_schema()
        baseline = ga._fingerprint("deepseek", "deepseek-v4-pro", prompt, USER, schema)
        self.assertEqual(
            baseline,
            ga._fingerprint("deepseek", "deepseek-v4-pro", prompt, USER, schema),
        )


class ApiKeyBoundaryTests(EnvCase):
    def test_missing_provider_keys_report_the_correct_variable(self) -> None:
        cases = [
            ("anthropic", "ANTHROPIC_API_KEY", "_lazy_anthropic_client"),
            ("openai", "OPENAI_API_KEY", "_openai_compatible_parse"),
            ("deepseek", "DEEPSEEK_API_KEY", "_openai_compatible_parse"),
        ]
        for provider, key, method in cases:
            with self.subTest(provider=provider):
                llm = ga.CachedLLM(provider=provider, cache_dir=self.make_cache_dir())
                with self.assertRaises(RuntimeError) as ctx:
                    if method == "_lazy_anthropic_client":
                        llm._lazy_anthropic_client()
                    else:
                        llm._openai_compatible_parse(SYSTEM, USER, ga.ExtractionResult)
                message = str(ctx.exception)
                self.assertIn(key, message)
                for other in set(ga.PROVIDER_KEY_ENV.values()) - {key}:
                    self.assertNotIn(other, message)
                self.assertNotIn("test-", message)


class CacheFingerprintTests(unittest.TestCase):
    def test_provider_and_model_are_part_of_fingerprint(self) -> None:
        schema = ga.Judgement.model_json_schema()
        same = ga._fingerprint("openai", "same-model", SYSTEM, USER, schema)
        self.assertEqual(
            same,
            ga._fingerprint("openai", "same-model", SYSTEM, USER, schema),
        )
        self.assertNotEqual(
            same,
            ga._fingerprint("deepseek", "same-model", SYSTEM, USER, schema),
        )
        self.assertNotEqual(
            same,
            ga._fingerprint("openai", "other-model", SYSTEM, USER, schema),
        )

    def test_deepseek_thinking_is_part_of_fingerprint(self) -> None:
        schema = ga.Judgement.model_json_schema()
        enabled = ga._fingerprint(
            "deepseek",
            "deepseek-v4-pro",
            SYSTEM,
            USER,
            schema,
            thinking="enabled",
            reasoning_effort="high",
        )
        disabled = ga._fingerprint(
            "deepseek",
            "deepseek-v4-pro",
            SYSTEM,
            USER,
            schema,
            thinking="disabled",
            reasoning_effort=None,
        )
        self.assertNotEqual(enabled, disabled)

    def test_deepseek_reasoning_effort_is_part_of_fingerprint(self) -> None:
        schema = ga.Judgement.model_json_schema()
        high = ga._fingerprint(
            "deepseek",
            "deepseek-v4-pro",
            SYSTEM,
            USER,
            schema,
            thinking="enabled",
            reasoning_effort="high",
        )
        max_effort = ga._fingerprint(
            "deepseek",
            "deepseek-v4-pro",
            SYSTEM,
            USER,
            schema,
            thinking="enabled",
            reasoning_effort="max",
        )
        self.assertNotEqual(high, max_effort)

    def test_cache_hit_does_not_create_client_or_increment_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            model = "same-model"
            digest = ga._fingerprint(
                "anthropic", model, SYSTEM, USER, ga.ExtractionResult.model_json_schema()
            )
            (cache_dir / f"{digest}.json").write_text(
                json.dumps({"parsed": extraction_payload()}), encoding="utf-8"
            )
            llm = ga.CachedLLM(provider="anthropic", model=model, cache_dir=cache_dir)
            with mock.patch.object(llm, "_lazy_anthropic_client") as client:
                result = llm.parse(SYSTEM, USER, ga.ExtractionResult)
            self.assertIsInstance(result, ga.ExtractionResult)
            self.assertEqual(llm.stats, {"hit": 1, "miss": 0})
            client.assert_not_called()

    def test_provider_specific_cache_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            model = "shared-model"
            digest = ga._fingerprint(
                "anthropic", model, SYSTEM, USER, ga.ExtractionResult.model_json_schema()
            )
            (cache_dir / f"{digest}.json").write_text(
                json.dumps({"parsed": extraction_payload()}), encoding="utf-8"
            )
            llm = ga.CachedLLM(
                offline=True, provider="openai", model=model, cache_dir=cache_dir
            )
            with self.assertRaisesRegex(RuntimeError, "cache|缓存|miss|未命中"):
                llm.parse(SYSTEM, USER, ga.ExtractionResult)

    def test_successful_call_writes_safe_cache_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            fake_client = types.SimpleNamespace(
                chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(
                        create=mock.Mock(return_value=openai_response(judgement_payload()))
                    )
                )
            )
            with mock.patch.dict(
                os.environ,
                {"OPENAI_API_KEY": fake_key("openai")},
                clear=True,
            ), mock.patch.dict(sys.modules, {"openai": openai_module(fake_client)}):
                llm = ga.CachedLLM(provider="openai", cache_dir=cache_dir)
                result = llm.parse(SYSTEM, USER, ga.Judgement)
            self.assertIsInstance(result, ga.Judgement)
            cache_files = list(cache_dir.glob("*.json"))
            self.assertEqual(len(cache_files), 1)
            cache_text = cache_files[0].read_text(encoding="utf-8")
            cache = json.loads(cache_text)
            self.assertEqual(cache["provider"], "openai")
            self.assertEqual(cache["model"], ga.DEFAULT_MODELS["openai"])
            self.assertEqual(cache["_schema_name"], "Judgement")
            self.assertEqual(cache["parsed"]["category"], "Configuration")
            self.assertNotIn(fake_key("openai"), cache_text)
            self.assertNotIn("Authorization", cache_text)

    def test_deepseek_cache_records_thinking_and_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            fake_client = types.SimpleNamespace(
                chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(
                        create=mock.Mock(return_value=openai_response(judgement_payload()))
                    )
                )
            )
            with mock.patch.dict(
                os.environ,
                {"DEEPSEEK_API_KEY": fake_key("deepseek")},
                clear=True,
            ), mock.patch.dict(sys.modules, {"openai": openai_module(fake_client)}):
                llm = ga.CachedLLM(provider="deepseek", cache_dir=cache_dir)
                result = llm.parse(SYSTEM, USER, ga.Judgement)
            self.assertIsInstance(result, ga.Judgement)
            cache_files = list(cache_dir.glob("*.json"))
            self.assertEqual(len(cache_files), 1)
            cache_text = cache_files[0].read_text(encoding="utf-8")
            cache = json.loads(cache_text)
            self.assertEqual(cache["provider"], "deepseek")
            self.assertEqual(cache["model"], "deepseek-v4-pro")
            self.assertEqual(cache["thinking"], "enabled")
            self.assertEqual(cache["reasoning_effort"], "high")
            self.assertEqual(cache["_request"]["thinking"], "enabled")
            self.assertEqual(cache["_request"]["reasoning_effort"], "high")
            self.assertNotIn(fake_key("deepseek"), cache_text)
            self.assertNotIn("Authorization", cache_text)


class ProviderRequestContractTests(EnvCase):
    def test_anthropic_contract_parses_extraction_and_judgement(self) -> None:
        for output_format, parsed in (
            (ga.ExtractionResult, ga.ExtractionResult.model_validate(extraction_payload())),
            (ga.Judgement, ga.Judgement.model_validate(judgement_payload())),
        ):
            with self.subTest(output_format=output_format.__name__):
                fake_client = types.SimpleNamespace(
                    messages=types.SimpleNamespace(
                        parse=mock.Mock(
                            return_value=types.SimpleNamespace(
                                parsed_output=parsed, stop_reason="end_turn"
                            )
                        )
                    )
                )
                with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                    os.environ, {"ANTHROPIC_API_KEY": fake_key("anthropic")}, clear=True
                ), mock.patch.dict(sys.modules, {"anthropic": anthropic_module(fake_client)}):
                    llm = ga.CachedLLM(
                        provider="anthropic", model="claude-test", cache_dir=Path(tmp)
                    )
                    result = llm.parse(SYSTEM, USER, output_format)
                self.assertIsInstance(result, output_format)
                kwargs = fake_client.messages.parse.call_args.kwargs
                self.assertEqual(kwargs["model"], "claude-test")
                self.assertEqual(kwargs["system"], SYSTEM)
                self.assertEqual(kwargs["messages"], [{"role": "user", "content": USER}])
                self.assertEqual(kwargs["max_tokens"], ga.MAX_TOKENS)
                self.assertIs(kwargs["output_format"], output_format)
                self.assertNotIn("extra_body", kwargs)
                self.assertNotIn("reasoning_effort", kwargs)

    def test_openai_contract_parses_extraction_and_judgement(self) -> None:
        for output_format, payload in (
            (ga.ExtractionResult, extraction_payload()),
            (ga.Judgement, judgement_payload()),
        ):
            with self.subTest(output_format=output_format.__name__):
                fake_create = mock.Mock(return_value=openai_response(payload))
                fake_client = types.SimpleNamespace(
                    chat=types.SimpleNamespace(
                        completions=types.SimpleNamespace(create=fake_create)
                    )
                )
                fake_module = openai_module(fake_client)
                with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                    os.environ, {"OPENAI_API_KEY": fake_key("openai")}, clear=True
                ), mock.patch.dict(sys.modules, {"openai": fake_module}):
                    llm = ga.CachedLLM(provider="openai", model="openai-test", cache_dir=Path(tmp))
                    result = llm.parse(SYSTEM, USER, output_format)
                self.assertIsInstance(result, output_format)
                fake_module.OpenAI.assert_called_once_with(
                    api_key=fake_key("openai"), base_url=ga.OPENAI_BASE_URL
                )
                kwargs = fake_create.call_args.kwargs
                self.assertEqual(kwargs["model"], "openai-test")
                self.assertEqual(kwargs["messages"][0], {"role": "system", "content": SYSTEM})
                self.assertEqual(kwargs["messages"][1], {"role": "user", "content": USER})
                self.assertEqual(kwargs["max_completion_tokens"], ga.MAX_TOKENS)
                self.assertNotIn("extra_body", kwargs)
                self.assertNotIn("reasoning_effort", kwargs)
                response_format = kwargs["response_format"]
                self.assertEqual(response_format["type"], "json_schema")
                self.assertEqual(response_format["json_schema"]["name"], output_format.__name__)
                self.assertEqual(
                    response_format["json_schema"]["schema"],
                    output_format.model_json_schema(),
                )

    def test_deepseek_contract_parses_extraction_and_judgement(self) -> None:
        for output_format, payload in (
            (ga.ExtractionResult, extraction_payload()),
            (ga.Judgement, judgement_payload()),
        ):
            with self.subTest(output_format=output_format.__name__):
                fake_create = mock.Mock(return_value=openai_response(payload))
                fake_client = types.SimpleNamespace(
                    chat=types.SimpleNamespace(
                        completions=types.SimpleNamespace(create=fake_create)
                    )
                )
                fake_module = openai_module(fake_client)
                with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                    os.environ, {"DEEPSEEK_API_KEY": fake_key("deepseek")}, clear=True
                ), mock.patch.dict(sys.modules, {"openai": fake_module}):
                    llm = ga.CachedLLM(
                        provider="deepseek", model="deepseek-test", cache_dir=Path(tmp)
                    )
                    result = llm.parse(SYSTEM, USER, output_format)
                self.assertIsInstance(result, output_format)
                fake_module.OpenAI.assert_called_once_with(
                    api_key=fake_key("deepseek"), base_url=ga.DEEPSEEK_BASE_URL
                )
                kwargs = fake_create.call_args.kwargs
                self.assertEqual(kwargs["model"], "deepseek-test")
                self.assertEqual(kwargs["response_format"], {"type": "json_object"})
                self.assertIn(SYSTEM, kwargs["messages"][0]["content"])
                self.assertIn("JSON Schema", kwargs["messages"][0]["content"])
                self.assertEqual(kwargs["messages"][1], {"role": "user", "content": USER})
                self.assertEqual(kwargs["max_tokens"], ga.MAX_TOKENS)
                self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "enabled"}})
                self.assertEqual(kwargs["reasoning_effort"], "high")

    def test_deepseek_disabled_thinking_omits_reasoning_effort(self) -> None:
        fake_create = mock.Mock(return_value=openai_response(judgement_payload()))
        fake_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=fake_create)
            )
        )
        fake_module = openai_module(fake_client)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": fake_key("deepseek"),
                "CARVEOPS_DEEPSEEK_THINKING": "disabled",
                "CARVEOPS_DEEPSEEK_REASONING_EFFORT": "max",
            },
            clear=True,
        ), mock.patch.dict(sys.modules, {"openai": fake_module}):
            llm = ga.CachedLLM(provider="deepseek", cache_dir=Path(tmp))
            result = llm.parse(SYSTEM, USER, ga.Judgement)
        self.assertIsInstance(result, ga.Judgement)
        self.assertEqual(llm.thinking, "disabled")
        self.assertIsNone(llm.reasoning_effort)
        kwargs = fake_create.call_args.kwargs
        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", kwargs)


class InvalidResponseTests(EnvCase):
    def _assert_invalid_openai_payload_is_rejected(self, content: str | dict) -> None:
        response = (
            types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(
                            content=content if isinstance(content, str) else json.dumps(content)
                        )
                    )
                ]
            )
        )
        fake_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=mock.Mock(return_value=response))
            )
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"OPENAI_API_KEY": fake_key("openai")}, clear=True
        ), mock.patch.dict(sys.modules, {"openai": openai_module(fake_client)}):
            llm = ga.CachedLLM(provider="openai", cache_dir=Path(tmp))
            with self.assertRaises((json.JSONDecodeError, ValidationError, ValueError)):
                llm.parse(SYSTEM, USER, ga.Judgement)
            self.assertEqual(list(Path(tmp).glob("*.json")), [])

    def test_invalid_structured_responses_are_rejected_and_not_cached(self) -> None:
        invalid_cases = [
            "not-json",
            {"category": "Configuration"},
            judgement_payload(category="CustomBuild"),
            judgement_payload(confidence="high"),
            judgement_payload(evidence_entry_ids="KB-XC-003"),
        ]
        for payload in invalid_cases:
            with self.subTest(payload=payload):
                self._assert_invalid_openai_payload_is_rejected(payload)


class OfflineAndMetadataTests(EnvCase):
    def test_offline_cache_hit_succeeds_without_key_or_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            digest = ga._fingerprint(
                "deepseek",
                ga.DEFAULT_MODELS["deepseek"],
                SYSTEM,
                USER,
                ga.Judgement.model_json_schema(),
                thinking=ga.DEFAULT_DEEPSEEK_THINKING,
                reasoning_effort=ga.DEFAULT_DEEPSEEK_REASONING_EFFORT,
            )
            (cache_dir / f"{digest}.json").write_text(
                json.dumps({"parsed": judgement_payload()}), encoding="utf-8"
            )
            llm = ga.CachedLLM(offline=True, provider="deepseek", cache_dir=cache_dir)
            with mock.patch.object(llm, "_lazy_openai_compatible_client") as client:
                result = llm.parse(SYSTEM, USER, ga.Judgement)
            self.assertIsInstance(result, ga.Judgement)
            self.assertEqual(llm.stats, {"hit": 1, "miss": 0})
            client.assert_not_called()

    def test_offline_cache_miss_does_not_use_existing_environment_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"OPENAI_API_KEY": fake_key("openai")}, clear=True
        ):
            llm = ga.CachedLLM(offline=True, provider="openai", cache_dir=Path(tmp))
            with mock.patch.object(llm, "_lazy_openai_compatible_client") as client:
                with self.assertRaisesRegex(RuntimeError, "缓存未命中|cache"):
                    llm.parse(SYSTEM, USER, ga.Judgement)
            client.assert_not_called()

    def test_build_report_meta_uses_selected_provider_and_model(self) -> None:
        class FakeRetriever:
            def query(self, text: str, n: int = ga.BASELINE_FETCH) -> list[dict]:
                return [
                    {
                        "entry_id": "KB-XC-003",
                        "section": "configuration",
                        "domain": "cross_cutting",
                        "similarity": 0.9,
                    }
                ]

            def top_entries(self, hits: list[dict], k: int) -> list[dict]:
                return hits[:k]

        fake_requirement = {
            "extracted_id": "EX-001",
            "source_note_id": "N-001",
            "requirement_description": "Use configurable thresholds.",
            "domain": "P2P",
            "source_quote": "Use configurable thresholds.",
        }
        with mock.patch.object(ga, "Retriever", return_value=FakeRetriever()), mock.patch.object(
            ga, "extract_requirements", return_value=[fake_requirement]
        ):
            llm = ga.CachedLLM(
                provider="openai", model="openai-meta", cache_dir=self.make_cache_dir()
            )
            report = ga.build_report(llm, baseline_only=True)
        self.assertEqual(report["_meta"]["provider"], "openai")
        self.assertEqual(report["_meta"]["model"], "openai-meta")
        self.assertNotIn("thinking", report["_meta"])
        self.assertNotIn("reasoning_effort", report["_meta"])

    def test_build_report_meta_records_deepseek_thinking_config(self) -> None:
        class FakeRetriever:
            def query(self, text: str, n: int = ga.BASELINE_FETCH) -> list[dict]:
                return [
                    {
                        "entry_id": "KB-XC-003",
                        "section": "configuration",
                        "domain": "cross_cutting",
                        "similarity": 0.9,
                    }
                ]

            def top_entries(self, hits: list[dict], k: int) -> list[dict]:
                return hits[:k]

        fake_requirement = {
            "extracted_id": "EX-001",
            "source_note_id": "N-001",
            "requirement_description": "Use configurable thresholds.",
            "domain": "P2P",
            "source_quote": "Use configurable thresholds.",
        }
        with mock.patch.object(ga, "Retriever", return_value=FakeRetriever()), mock.patch.object(
            ga, "extract_requirements", return_value=[fake_requirement]
        ):
            llm = ga.CachedLLM(provider="deepseek", cache_dir=self.make_cache_dir())
            report = ga.build_report(llm, baseline_only=True)
        self.assertEqual(report["_meta"]["provider"], "deepseek")
        self.assertEqual(report["_meta"]["model"], "deepseek-v4-pro")
        self.assertEqual(report["_meta"]["thinking"], "enabled")
        self.assertEqual(report["_meta"]["reasoning_effort"], "high")

    def test_cache_stats_do_not_affect_gap_report_content_sha(self) -> None:
        report = {
            "_meta": {
                "module": "module_2_fit_to_standard_gap_analysis",
                "llm_cache": {
                    "dir": "data/synthetic/llm_cache",
                    "note_zh": "缓存进 git；填充后重跑完全离线且字节一致。",
                },
            },
            "requirements": [
                {
                    "extracted_id": "EX-001",
                    "requirement_description": "Use configurable thresholds.",
                    "llm": {"category": "Configuration"},
                }
            ],
            "dev_backlog": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            online = ga.CachedLLM(provider="deepseek", cache_dir=Path(tmp))
            offline = ga.CachedLLM(provider="deepseek", offline=True, cache_dir=Path(tmp))
        online.stats = {"hit": 0, "miss": 44}
        offline.stats = {"hit": 44, "miss": 0}

        online_report = ga.attach_gap_analysis_run_info(report, online)
        offline_report = ga.attach_gap_analysis_run_info(report, offline)

        self.assertEqual(
            online_report["_run_info"]["content_sha256"],
            offline_report["_run_info"]["content_sha256"],
        )
        self.assertEqual(online_report["_run_info"]["llm_cache"], {"hits": 0, "misses": 44})
        self.assertEqual(offline_report["_run_info"]["llm_cache"], {"hits": 44, "misses": 0})
        self.assertNotIn("hits", online_report["_meta"]["llm_cache"])
        self.assertNotIn("misses", online_report["_meta"]["llm_cache"])

        changed = json.loads(json.dumps(report))
        changed["requirements"][0]["llm"]["category"] = "Fit"
        changed_report = ga.attach_gap_analysis_run_info(changed, online)
        self.assertNotEqual(
            online_report["_run_info"]["content_sha256"],
            changed_report["_run_info"]["content_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
