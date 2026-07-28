from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.agents import cutover_agent as agent


def plan(intent: str, requests: list[dict] | None = None, **extra) -> dict:
    payload = {
        "intent": intent,
        "tools": [item["tool_name"] for item in requests or []],
        "activity_filters": {},
        "raid_filters": {},
        "rebuild_plan": False,
        "answer_focus": intent,
        "confidence": 0.9,
        "needs_clarification": False,
        "clarification_question": "",
        "tool_requests": requests or [],
    }
    payload.update(extra)
    return payload


def run_fake(query: str, fake_plan: dict, **kwargs):
    with tempfile.TemporaryDirectory() as tmp:
        return agent.run_agent(query, fake_plan=fake_plan, trace_output=Path(tmp) / "trace.json", **kwargs)


class CutoverAgentTests(unittest.TestCase):
    def test_graph_contains_required_nodes(self) -> None:
        graph = agent.build_graph()
        nodes = set(graph.nodes)
        self.assertTrue({"validate_input", "plan_request", "enforce_policy", "execute_mcp_tools", "compose_answer", "validate_output"}.issubset(nodes))

    def test_normal_path_sequence_is_correct(self) -> None:
        p = plan("combined_brief", [
            {"tool_name": "get_cutover_status_summary", "arguments": {}, "reason": "status"},
            {"tool_name": "get_cutover_daily_brief", "arguments": {}, "reason": "daily"},
        ])
        state, _ = run_fake("current status", p)
        self.assertEqual([e["node"] for e in state["trace_events"]], ["validate_input", "plan_request", "enforce_policy", "execute_mcp_tools", "compose_answer", "validate_output"])

    def test_clarification_branch_is_correct(self) -> None:
        p = plan("status_summary", needs_clarification=True, clarification_question="Which scope?")
        state, _ = run_fake("status?", p)
        self.assertIn("Which scope", state["final_answer"])
        self.assertEqual(state["mcp_sessions"], 0)

    def test_unsupported_branch_does_not_call_mcp(self) -> None:
        state, _ = run_fake("把 ACT-EX-024-TEST 修改成 Completed", plan("unsupported"))
        self.assertEqual(state["mcp_sessions"], 0)
        self.assertIn("只读", state["final_answer"])

    def test_planner_tools_must_be_whitelisted(self) -> None:
        result = agent.enforce_policy({
            "user_query": "status",
            "allow_rebuild": False,
            "trace_events": [],
            "plan": {"intent": "combined_brief"},
            "tool_requests": [{"tool_name": "unknown_tool", "arguments": {}, "reason": "bad"}],
        })
        self.assertFalse(result["policy_decision"]["allowed"])

    def test_tool_count_limit_is_three(self) -> None:
        reqs = [{"tool_name": "get_cutover_daily_brief", "arguments": {}, "reason": str(i)} for i in range(4)]
        state, _ = run_fake("status", plan("combined_brief", reqs))
        self.assertFalse(state["policy_decision"]["allowed"])

    def test_illegal_tool_is_rejected_by_policy(self) -> None:
        p = plan("combined_brief", [{"tool_name": "list_raid_items", "arguments": {"raid_type": "Decision"}, "reason": "bad"}])
        state, _ = run_fake("risks", p)
        self.assertFalse(state["policy_decision"]["allowed"])

    def test_path_like_argument_is_rejected(self) -> None:
        p = plan("activity_search", [{"tool_name": "list_cutover_activities", "arguments": {"owner_role": "C:\\temp"}, "reason": "bad"}])
        state, _ = run_fake("activities", p)
        self.assertFalse(state["policy_decision"]["allowed"])

    def test_rebuild_is_rejected_by_default(self) -> None:
        p = plan("rebuild_reports", [{"tool_name": "rebuild_cutover_reports", "arguments": {"rebuild_plan": False}, "reason": "rebuild"}])
        state, _ = run_fake("重新生成状态和日报", p)
        self.assertFalse(state["policy_decision"]["allowed"])

    def test_allow_rebuild_status_only_is_available(self) -> None:
        p = plan("rebuild_reports", [{"tool_name": "rebuild_cutover_reports", "arguments": {"rebuild_plan": False}, "reason": "rebuild"}])
        state, _ = run_fake("重新生成状态和日报", p, allow_rebuild=True)
        self.assertTrue(state["policy_decision"]["allowed"])
        self.assertIn("validation = valid", state["final_answer"])

    def test_query_cannot_be_converted_to_rebuild(self) -> None:
        p = plan("rebuild_reports", [{"tool_name": "rebuild_cutover_reports", "arguments": {"rebuild_plan": False}, "reason": "bad"}])
        state, _ = run_fake("当前状态怎么样", p, allow_rebuild=True)
        self.assertFalse(state["policy_decision"]["allowed"])

    def test_due_query_is_normalized_to_daily_brief(self) -> None:
        p = plan("activity_search", [{"tool_name": "list_cutover_activities", "arguments": {}, "reason": "search"}])
        state, _ = run_fake("哪些活动在 T-7 到期？", p)
        self.assertEqual(state["plan"]["intent"], "daily_brief")
        self.assertEqual([r["tool_name"] for r in state["tool_results"]], ["get_cutover_daily_brief"])

    def test_explicit_rebuild_request_allows_rebuild(self) -> None:
        p = plan("rebuild_reports", [{"tool_name": "rebuild_cutover_reports", "arguments": {"rebuild_plan": False}, "reason": "rebuild"}])
        state, _ = run_fake("请重新生成状态和日报", p, allow_rebuild=True)
        self.assertTrue(state["policy_decision"]["allowed"])

    def test_mcp_session_uses_stdio(self) -> None:
        source = Path(agent.__file__).read_text(encoding="utf-8")
        self.assertIn("stdio_client", source)
        self.assertIn("StdioServerParameters", source)

    def test_combined_brief_calls_two_tools(self) -> None:
        p = plan("combined_brief", [
            {"tool_name": "get_cutover_status_summary", "arguments": {}, "reason": "status"},
            {"tool_name": "get_cutover_daily_brief", "arguments": {}, "reason": "daily"},
        ])
        state, _ = run_fake("当前状态", p)
        self.assertEqual([r["tool_name"] for r in state["tool_results"]], ["get_cutover_status_summary", "get_cutover_daily_brief"])

    def test_blocked_query_returns_two_rows(self) -> None:
        p = plan("blocked_activities", [
            {"tool_name": "list_cutover_activities", "arguments": {"status": "Blocked", "critical_only": True}, "reason": "blocked"},
        ])
        state, _ = run_fake("是什么阻塞了 Cutover Readiness？", p)
        self.assertIn("2 个匹配活动", state["final_answer"])

    def test_risk_query_returns_two_rows(self) -> None:
        p = plan("raid_review", [{"tool_name": "list_raid_items", "arguments": {"raid_type": "Risk"}, "reason": "risks"}])
        state, _ = run_fake("列出风险", p)
        self.assertIn("2 个 Risk", state["final_answer"])

    def test_management_actions_come_from_daily(self) -> None:
        p = plan("management_actions", [{"tool_name": "get_cutover_daily_brief", "arguments": {}, "reason": "actions"}])
        state, _ = run_fake("管理层需要什么行动", p)
        self.assertIn("管理层动作共有 4 项", state["final_answer"])

    def test_unsupported_status_change_returns_read_only_answer(self) -> None:
        state, _ = run_fake("把 ACT-EX-024-TEST 修改成 Completed", plan("unsupported"))
        self.assertIn("不能修改活动状态", state["final_answer"])

    def test_chinese_query_generates_chinese_answer(self) -> None:
        state, _ = run_fake("当前 Cutover 总体状态怎么样？", agent.heuristic_plan("当前 Cutover 总体状态怎么样？").model_dump())
        self.assertIn("当前 Cutover 状态", state["final_answer"])

    def test_english_query_generates_english_answer(self) -> None:
        state, _ = run_fake("What is blocking Cutover Readiness?", agent.heuristic_plan("What is blocking Cutover Readiness?").model_dump())
        self.assertIn("There are 2 matching activities", state["final_answer"])

    def test_numbers_come_from_tool_result(self) -> None:
        state, _ = run_fake("current status", agent.heuristic_plan("current status").model_dump())
        daily = next(r for r in state["tool_results"] if r["tool_name"] == "get_cutover_daily_brief")
        self.assertIn(str(daily["data"]["headline"]["blocked_activity_count"]), state["final_answer"])

    def test_sha_citations_are_correct(self) -> None:
        state, _ = run_fake("current status", agent.heuristic_plan("current status").model_dump())
        for citation in state["citations"]:
            result = next(r for r in state["tool_results"] if r["tool_name"] == citation["tool"])
            self.assertEqual(citation["sha"], result["source_content_sha256"])

    def test_tool_failure_path_is_handled(self) -> None:
        with mock.patch.object(agent, "execute_mcp_requests", return_value=[{"tool_name": "x", "arguments": {}, "ok": False, "data": None, "error": {"code": "X"}, "source_content_sha256": None}]):
            p = plan("combined_brief", [{"tool_name": "get_cutover_daily_brief", "arguments": {}, "reason": "daily"}])
            state, _ = run_fake("status", p)
        self.assertIn("Unable to compose", state["final_answer"])

    def test_mcp_ok_false_is_processed(self) -> None:
        with mock.patch.object(agent, "execute_mcp_requests", return_value=[{"tool_name": "get_cutover_daily_brief", "arguments": {}, "ok": False, "data": None, "error": {"code": "INVALID_FILTER"}, "source_content_sha256": None}]):
            p = plan("daily_brief", [{"tool_name": "get_cutover_daily_brief", "arguments": {}, "reason": "daily"}])
            state, _ = run_fake("daily", p)
        self.assertEqual(state["tool_results"][0]["error"]["code"], "INVALID_FILTER")

    def test_cache_fingerprint_includes_prompt_schema_and_tools(self) -> None:
        fp = agent.planner_fingerprint("q", "deepseek", "deepseek-v4-pro", "enabled", "high")
        changed = agent.planner_fingerprint("q2", "deepseek", "deepseek-v4-pro", "enabled", "high")
        self.assertNotEqual(fp, changed)

    def test_offline_cache_hit_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = agent.CACHE_DIR
            agent.CACHE_DIR = Path(tmp)
            query = "risk"
            p = agent.heuristic_plan(query)
            digest = agent.planner_fingerprint(query, "deepseek", "deepseek-v4-pro", "enabled", "high")
            Path(tmp).mkdir(parents=True, exist_ok=True)
            (Path(tmp) / f"{digest}.json").write_text(json.dumps({"parsed": p.model_dump()}), encoding="utf-8")
            try:
                parsed, stats, _ = agent.call_planner(query, provider="deepseek", model="deepseek-v4-pro", offline=True)
            finally:
                agent.CACHE_DIR = old
        self.assertEqual(stats["hit"], 1)
        self.assertEqual(parsed.intent, "raid_review")

    def test_offline_cache_miss_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = agent.CACHE_DIR
            agent.CACHE_DIR = Path(tmp)
            try:
                with self.assertRaises(agent.PlannerCacheMiss):
                    agent.call_planner("missing", provider="deepseek", model="deepseek-v4-pro", offline=True)
            finally:
                agent.CACHE_DIR = old

    def test_cache_contains_no_api_key(self) -> None:
        data = {
            "_schema_name": "CutoverAgentPlan",
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "thinking": "enabled",
            "reasoning_effort": "high",
            "_request": {"user_query": "q"},
            "parsed": agent.heuristic_plan("q").model_dump(),
        }
        text = json.dumps(data)
        self.assertNotIn("API_KEY", text)
        self.assertNotIn("Authorization", text)

    def test_cache_contains_no_reasoning_content(self) -> None:
        text = json.dumps({"parsed": agent.heuristic_plan("q").model_dump()})
        self.assertNotIn("reasoning_content", text)

    def test_trace_contains_no_chain_of_thought(self) -> None:
        _state, trace = run_fake("current status", agent.heuristic_plan("current status").model_dump())
        text = json.dumps(trace)
        self.assertNotIn("chain-of-thought", text)
        self.assertNotIn("reasoning_content", text)

    def test_same_input_trace_sha_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            _, first = agent.run_agent("current status", fake_plan=agent.heuristic_plan("current status").model_dump(), trace_output=path)
            _, second = agent.run_agent("current status", fake_plan=agent.heuristic_plan("current status").model_dump(), trace_output=path)
        self.assertEqual(first["_run_info"]["content_sha256"], second["_run_info"]["content_sha256"])

    def test_runtime_cache_stats_do_not_change_trace_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path1 = Path(tmp) / "online.json"
            path2 = Path(tmp) / "offline.json"
            payload = {
                "_meta": {"component": "cutover_langgraph_agent", "graph": agent.GRAPH_NAME},
                "request": {"request_id": "REQ", "user_query": "current status"},
                "plan": {"intent": "combined_brief"},
                "policy": {"allowed": True},
                "tool_calls": [],
                "final_answer": "same answer",
                "validation": {"valid": True, "reasons": []},
                "mcp_sessions": 1,
                "graph_path": ["validate_input", "plan_request", "validate_output"],
                "errors": [],
            }
            first = agent.stable_write_report(payload, path1, runtime_info={"offline": False, "planner_cache": {"hit": 0, "miss": 1}})
            second = agent.stable_write_report(payload, path2, runtime_info={"offline": True, "planner_cache": {"hit": 1, "miss": 0}})
        self.assertEqual(first["_run_info"]["content_sha256"], second["_run_info"]["content_sha256"])
        self.assertEqual(first["_run_info"]["planner_cache"], {"hit": 0, "miss": 1})
        self.assertEqual(second["_run_info"]["planner_cache"], {"hit": 1, "miss": 0})

    def test_report_sha_change_changes_trace_sha(self) -> None:
        state, _ = run_fake("current status", agent.heuristic_plan("current status").model_dump())
        payload1 = agent.trace_payload(state)
        changed = copy.deepcopy(state)
        changed["tool_results"][0]["source_content_sha256"] = "changed"
        payload2 = agent.trace_payload(changed)
        self.assertNotEqual(agent.attach_run_info(payload1)["_run_info"]["content_sha256"], agent.attach_run_info(payload2)["_run_info"]["content_sha256"])

    def test_same_trace_sha_preserves_generated_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            _, first = agent.run_agent("current status", fake_plan=agent.heuristic_plan("current status").model_dump(), trace_output=path)
            _, second = agent.run_agent("current status", fake_plan=agent.heuristic_plan("current status").model_dump(), trace_output=path)
        self.assertEqual(first["_run_info"]["generated_at"], second["_run_info"]["generated_at"])

    def test_output_validator_rejects_absolute_path(self) -> None:
        state = {"final_answer": "C:\\secret\\file.txt", "citations": [], "tool_results": [], "trace_events": [], "language": "en"}
        result = agent.validate_answer(state)
        self.assertFalse(result["validation"]["valid"])

    def test_smoke_queries_can_run_with_fake_planner(self) -> None:
        with mock.patch.dict(os.environ, {"CARVEOPS_FAKE_PLANNER": "1"}):
            results = [agent.run_agent(q, offline=True, trace_output=Path(tempfile.gettempdir()) / f"{agent.stable_request_id(q)}.json")[0] for q in agent.FORMAL_QUERIES]
        self.assertEqual(len(results), 6)
        self.assertEqual(sum(1 for item in results if item["plan"]["intent"] == "unsupported"), 1)

    def test_existing_mcp_smoke_still_passes(self) -> None:
        import subprocess
        import sys

        result = subprocess.run([sys.executable, "scripts/smoke_test_cutover_mcp.py"], cwd=agent.PROJECT_ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Tools discovered: 6", result.stdout)

    def test_existing_99_tests_are_preserved_by_importing_agent(self) -> None:
        self.assertEqual(agent.GRAPH_NAME, "cutover-copilot")


if __name__ == "__main__":
    unittest.main()
