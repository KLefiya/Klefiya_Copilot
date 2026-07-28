"""Offline smoke test for the Cutover LangGraph Agent.

By default this replays the six formal queries from planner cache and calls the
local stdio MCP server for deterministic business data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agents.cutover_agent import FORMAL_QUERIES, run_formal_queries  # noqa: E402


def main() -> int:
    try:
        results = run_formal_queries(offline=True, allow_rebuild=False)
    except Exception as error:  # noqa: BLE001
        print(f"Cutover agent smoke test failed: {error}", file=sys.stderr)
        return 1

    cache_hit = sum(item["state"].get("planner_cache", {}).get("hit", 0) for item in results)
    cache_miss = sum(item["state"].get("planner_cache", {}).get("miss", 0) for item in results)
    mcp_sessions = len(results)
    unsupported = sum(1 for item in results if item["state"].get("plan", {}).get("intent") == "unsupported")
    policy_violations = sum(
        1
        for item in results
        if item["state"].get("policy_decision", {}).get("allowed") is False
    )
    all_results = [
        result
        for item in results
        for result in item["state"].get("tool_results", [])
        if result.get("ok")
    ]
    overall = next(
        (
            result["data"]["overall_rag"]
            for result in all_results
            if result["tool_name"] == "get_cutover_daily_brief"
        ),
        "unknown",
    )
    blocked = next(
        (
            result["data"]["count"]
            for result in all_results
            if result["tool_name"] == "list_cutover_activities"
            and result["arguments"].get("status") == "Blocked"
        ),
        0,
    )
    validation = "valid" if all(item["state"].get("validation", {}).get("valid") for item in results) else "invalid"

    print("Agent graph: cutover-copilot")
    print(f"Queries: {len(FORMAL_QUERIES)}")
    print(f"Planner cache: {cache_hit} hit / {cache_miss} miss")
    print(f"MCP sessions: {mcp_sessions}")
    print(f"Unsupported requests: {unsupported}")
    print(f"Policy violations: {policy_violations}")
    print(f"Overall status observed: {overall}")
    print(f"Blocked activities observed: {blocked}")
    print(f"Validation: {validation}")
    return 0 if validation == "valid" and cache_hit == len(FORMAL_QUERIES) and cache_miss == 0 else 1


if __name__ == "__main__":
    os.environ.pop("DEEPSEEK_API_KEY", None)
    raise SystemExit(main())
