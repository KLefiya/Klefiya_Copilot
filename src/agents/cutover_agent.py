"""LangGraph Cutover Copilot agent backed by the local stdio MCP server.

The planner may call DeepSeek to choose tools, but all cutover business data is
retrieved through the stdio MCP server. Final answers are deterministic
templates over MCP tool data; no LLM is used to compose answers.

Usage:
    python -m src.agents.cutover_agent --query "What is blocking Cutover Readiness?"
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = PROJECT_ROOT / "src" / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from data_profile import attach_run_info  # noqa: E402
from gap_analysis import (  # noqa: E402
    DEFAULT_DEEPSEEK_REASONING_EFFORT,
    DEFAULT_DEEPSEEK_THINKING,
    DEFAULT_MODELS,
    _deepseek_config_from_env,
    _safe_base_url,
)

CACHE_DIR = PROJECT_ROOT / "data" / "synthetic" / "cutover_agent_cache"
RUNS_DIR = PROJECT_ROOT / "data" / "synthetic" / "cutover_agent_runs"
DEFAULT_TRACE_PATH = PROJECT_ROOT / "data" / "synthetic" / "cutover_agent_trace.json"
SERVER_MODULE = "src.mcp_servers.cutover_server"
GRAPH_NAME = "cutover-copilot"
SCHEMA_VERSION = "cutover-agent-plan-v1"
MAX_TOOL_CALLS = 3
MAX_STRING_ARG = 120

ALLOWED_TOOLS = (
    "get_cutover_plan_summary",
    "get_cutover_status_summary",
    "get_cutover_daily_brief",
    "list_cutover_activities",
    "list_raid_items",
    "rebuild_cutover_reports",
)
TOOL_ARGUMENTS = {
    "get_cutover_plan_summary": set(),
    "get_cutover_status_summary": set(),
    "get_cutover_daily_brief": set(),
    "list_cutover_activities": {"status", "owner_role", "workstream", "critical_only"},
    "list_raid_items": {"raid_type", "status", "severity"},
    "rebuild_cutover_reports": {"rebuild_plan"},
}
ACTIVITY_STATUSES = {"Not Started", "In Progress", "Blocked", "Completed", "Cancelled"}
RAID_TYPES = {"Risk", "Assumption", "Issue", "Dependency"}
RAID_STATUSES = {"Open", "Mitigating", "Accepted", "Resolved", "Closed"}
RAID_SEVERITIES = {"Low", "Medium", "High", "Critical"}
INTENTS = (
    "plan_summary",
    "status_summary",
    "daily_brief",
    "blocked_activities",
    "activity_search",
    "raid_review",
    "management_actions",
    "rebuild_reports",
    "combined_brief",
    "unsupported",
)

FORMAL_QUERIES = (
    "当前 Cutover 总体状态怎么样？",
    "是什么阻塞了 Cutover Readiness？",
    "列出目前所有风险。",
    "哪些活动在 T-7 到期？",
    "接下来管理层需要采取什么行动？",
    "把 ACT-EX-024-TEST 修改成 Completed。",
)


class ActivityFilters(BaseModel):
    status: Literal["Not Started", "In Progress", "Blocked", "Completed", "Cancelled"] | None = None
    owner_role: str | None = None
    workstream: str | None = None
    critical_only: bool = False


class RaidFilters(BaseModel):
    raid_type: Literal["Risk", "Assumption", "Issue", "Dependency"] | None = None
    status: Literal["Open", "Mitigating", "Accepted", "Resolved", "Closed"] | None = None
    severity: Literal["Low", "Medium", "High", "Critical"] | None = None


class ToolRequest(BaseModel):
    tool_name: Literal[
        "get_cutover_plan_summary",
        "get_cutover_status_summary",
        "get_cutover_daily_brief",
        "list_cutover_activities",
        "list_raid_items",
        "rebuild_cutover_reports",
    ]
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str


class CutoverAgentPlan(BaseModel):
    intent: Literal[
        "plan_summary",
        "status_summary",
        "daily_brief",
        "blocked_activities",
        "activity_search",
        "raid_review",
        "management_actions",
        "rebuild_reports",
        "combined_brief",
        "unsupported",
    ]
    tools: list[str] = Field(default_factory=list)
    activity_filters: ActivityFilters = Field(default_factory=ActivityFilters)
    raid_filters: RaidFilters = Field(default_factory=RaidFilters)
    rebuild_plan: bool = False
    answer_focus: str = ""
    confidence: float = 0.0
    needs_clarification: bool = False
    clarification_question: str = ""
    tool_requests: list[ToolRequest] = Field(default_factory=list)


class CutoverAgentState(TypedDict, total=False):
    request_id: str
    user_query: str
    provider: str
    model: str
    offline: bool
    allow_rebuild: bool
    trace_output: str
    plan: dict[str, Any]
    policy_decision: dict[str, Any]
    tool_requests: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    final_answer: str
    citations: list[dict[str, str]]
    errors: list[dict[str, Any]]
    trace_events: list[dict[str, Any]]
    planner_cache: dict[str, int]
    mcp_sessions: int
    language: str
    validation: dict[str, Any]
    next_route: str
    fake_plan: dict[str, Any]


def detect_language(query: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", query) else "en"


def stable_request_id(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


def query_run_path(query: str) -> Path:
    return RUNS_DIR / f"{stable_request_id(query)}.json"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def has_forbidden_path_value(value: Any) -> bool:
    if isinstance(value, str):
        if len(value) > MAX_STRING_ARG:
            return True
        return bool(re.search(r"(^[A-Za-z]:\\)|(^/)|(\.\.)|[/\\]", value))
    if isinstance(value, dict):
        return any(has_forbidden_path_value(v) for v in value.values())
    if isinstance(value, list):
        return any(has_forbidden_path_value(v) for v in value)
    return False


def planner_system_prompt() -> str:
    return (
        "You are a controlled planner for a Cutover governance agent. "
        "Return only one JSON object matching the supplied schema. "
        "Do not answer the user. Do not invent tool names. Do not modify status. "
        "Use rebuild_cutover_reports only when the user explicitly asks to rebuild reports; "
        "a vague request to update or refresh is not enough. "
        "Unsupported status modification requests must use intent unsupported and no tool calls. "
        "Choose the smallest sufficient tool set, maximum three tools. "
        "Management actions must come from get_cutover_daily_brief. "
        "Do not output internal reasoning."
    )


def planner_user_prompt(query: str) -> str:
    schema = CutoverAgentPlan.model_json_schema()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "user_query": query,
        "allowed_tools": ALLOWED_TOOLS,
        "allowed_intents": INTENTS,
        "tool_argument_schema": {name: sorted(args) for name, args in TOOL_ARGUMENTS.items()},
        "examples": [
            {
                "query": "当前 Cutover 总体状态怎么样？",
                "intent": "combined_brief",
                "tools": ["get_cutover_status_summary", "get_cutover_daily_brief"],
            },
            {
                "query": "What is blocking Cutover Readiness?",
                "intent": "blocked_activities",
                "tools": ["list_cutover_activities", "get_cutover_daily_brief"],
                "arguments": {"status": "Blocked", "critical_only": True},
            },
            {
                "query": "列出所有风险",
                "intent": "raid_review",
                "tools": ["list_raid_items"],
                "arguments": {"raid_type": "Risk"},
            },
        ],
        "output_schema": schema,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def planner_fingerprint(
    query: str,
    provider: str,
    model: str,
    thinking: str | None,
    reasoning_effort: str | None,
) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "system": planner_system_prompt(),
        "user_query": query,
        "provider": provider,
        "model": model,
        "thinking": thinking,
        "reasoning_effort": reasoning_effort,
        "schema": CutoverAgentPlan.model_json_schema(),
        "allowed_tools": ALLOWED_TOOLS,
    }
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def parse_json_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return json.loads(stripped)


class PlannerCacheMiss(RuntimeError):
    pass


def call_planner(
    query: str,
    *,
    provider: str,
    model: str,
    offline: bool,
) -> tuple[CutoverAgentPlan, dict[str, int], str]:
    if provider != "deepseek":
        raise RuntimeError("Only provider=deepseek is currently supported for the Cutover planner.")
    thinking, reasoning_effort = _deepseek_config_from_env()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = planner_fingerprint(query, provider, model, thinking, reasoning_effort)
    cache_file = CACHE_DIR / f"{digest}.json"
    stats = {"hit": 0, "miss": 0}
    if cache_file.exists():
        stats["hit"] = 1
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        return CutoverAgentPlan.model_validate(cached["parsed"]), stats, digest
    if offline:
        raise PlannerCacheMiss(f"Planner cache miss for {digest[:16]} in offline mode.")

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is required for an uncached Planner call.")

    from openai import OpenAI

    stats["miss"] = 1
    client = OpenAI(api_key=api_key, base_url=_safe_base_url(provider))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": planner_system_prompt()},
            {"role": "user", "content": planner_user_prompt(query)},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 2048,
        "extra_body": {"thinking": {"type": thinking}},
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    response = client.chat.completions.create(**payload)
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise RuntimeError("DeepSeek Planner returned no choices.")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not content:
        raise RuntimeError("DeepSeek Planner returned empty content.")
    parsed = CutoverAgentPlan.model_validate(parse_json_text(content))
    cache_payload = {
        "_schema_name": "CutoverAgentPlan",
        "provider": provider,
        "model": model,
        "thinking": thinking,
        "reasoning_effort": reasoning_effort,
        "_request": {
            "schema_version": SCHEMA_VERSION,
            "system": planner_system_prompt(),
            "user_query": query,
            "allowed_tools": list(ALLOWED_TOOLS),
        },
        "parsed": parsed.model_dump(),
    }
    cache_file.write_text(json.dumps(cache_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return parsed, stats, digest


def heuristic_plan(query: str) -> CutoverAgentPlan:
    """Deterministic fallback used only for tests or manual cache seeding."""
    lower = query.lower()
    if any(term in query for term in ("修改", "改成", "写入", "更新状态")) or "completed" in lower and "change" in lower:
        return CutoverAgentPlan(
            intent="unsupported",
            answer_focus="status modification is read-only",
            confidence=0.95,
            tool_requests=[],
        )
    if "风险" in query or "risk" in lower:
        return CutoverAgentPlan(
            intent="raid_review",
            raid_filters=RaidFilters(raid_type="Risk"),
            answer_focus="risk items",
            confidence=0.9,
            tool_requests=[ToolRequest(tool_name="list_raid_items", arguments={"raid_type": "Risk"}, reason="List Risk RAID items.")],
        )
    if "阻塞" in query or "blocking" in lower or "blocked" in lower:
        return CutoverAgentPlan(
            intent="blocked_activities",
            activity_filters=ActivityFilters(status="Blocked", critical_only=True),
            answer_focus="blocked critical activities",
            confidence=0.9,
            tool_requests=[
                ToolRequest(tool_name="list_cutover_activities", arguments={"status": "Blocked", "critical_only": True}, reason="Find blocked activities."),
                ToolRequest(tool_name="get_cutover_daily_brief", arguments={}, reason="Get daily context and gate information."),
            ],
        )
    if "t-7" in lower or "到期" in query or "due" in lower:
        return CutoverAgentPlan(
            intent="daily_brief",
            answer_focus="due activities",
            confidence=0.85,
            tool_requests=[ToolRequest(tool_name="get_cutover_daily_brief", arguments={}, reason="Use due_now from the daily brief.")],
        )
    if "管理" in query or "action" in lower or "management" in lower:
        return CutoverAgentPlan(
            intent="management_actions",
            answer_focus="management actions",
            confidence=0.85,
            tool_requests=[ToolRequest(tool_name="get_cutover_daily_brief", arguments={}, reason="Use deterministic management actions.")],
        )
    if "rebuild" in lower or "重新生成" in query or "重建" in query:
        return CutoverAgentPlan(
            intent="rebuild_reports",
            rebuild_plan=False,
            answer_focus="rebuild reports",
            confidence=0.85,
            tool_requests=[ToolRequest(tool_name="rebuild_cutover_reports", arguments={"rebuild_plan": False}, reason="Explicit rebuild request.")],
        )
    return CutoverAgentPlan(
        intent="combined_brief",
        answer_focus="overall status",
        confidence=0.85,
        tool_requests=[
            ToolRequest(tool_name="get_cutover_status_summary", arguments={}, reason="Get status counts."),
            ToolRequest(tool_name="get_cutover_daily_brief", arguments={}, reason="Get RAG, blockers, and next gate."),
        ],
    )


def _filter_args(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, False, "", [], {})}


def tool_requests_from_plan(plan: CutoverAgentPlan) -> list[ToolRequest]:
    if plan.intent == "unsupported":
        return []
    if plan.tool_requests:
        return plan.tool_requests
    requests: list[ToolRequest] = []
    tool_names = list(plan.tools)
    if not tool_names:
        defaults = {
            "combined_brief": ["get_cutover_status_summary", "get_cutover_daily_brief"],
            "status_summary": ["get_cutover_status_summary"],
            "daily_brief": ["get_cutover_daily_brief"],
            "blocked_activities": ["list_cutover_activities", "get_cutover_daily_brief"],
            "activity_search": ["list_cutover_activities"],
            "raid_review": ["list_raid_items"],
            "management_actions": ["get_cutover_daily_brief"],
            "rebuild_reports": ["rebuild_cutover_reports"],
            "plan_summary": ["get_cutover_plan_summary"],
        }
        tool_names = defaults.get(plan.intent, [])
    for name in tool_names:
        args: dict[str, Any] = {}
        if name == "list_cutover_activities":
            args = _filter_args(plan.activity_filters.model_dump())
            if plan.intent == "blocked_activities":
                args.setdefault("status", "Blocked")
                args.setdefault("critical_only", True)
        elif name == "list_raid_items":
            args = _filter_args(plan.raid_filters.model_dump())
            if plan.intent == "raid_review":
                args.setdefault("raid_type", "Risk")
        elif name == "rebuild_cutover_reports":
            args = {"rebuild_plan": plan.rebuild_plan}
        requests.append(ToolRequest(tool_name=name, arguments=args, reason=f"Planner selected {name} for {plan.intent}."))
    return requests[:MAX_TOOL_CALLS + 1]


def normalize_plan_for_query(query: str, plan: CutoverAgentPlan) -> CutoverAgentPlan:
    lower = query.lower()
    if plan.intent != "unsupported" and ("t-7" in lower or "到期" in query or "due" in lower):
        return CutoverAgentPlan(
            intent="daily_brief",
            tools=["get_cutover_daily_brief"],
            answer_focus="due activities",
            confidence=max(plan.confidence, 0.9),
            tool_requests=[
                ToolRequest(
                    tool_name="get_cutover_daily_brief",
                    arguments={},
                    reason="Due-date questions are answered from the deterministic daily brief.",
                )
            ],
        )
    return plan


def validate_input(state: CutoverAgentState) -> CutoverAgentState:
    query = state.get("user_query", "").strip()
    events = state.get("trace_events", [])
    errors = state.get("errors", [])
    if not query:
        errors.append({"code": "INVALID_INPUT", "message": "Query must not be empty."})
        route = "error"
    else:
        route = "continue"
    events.append({"node": "validate_input", "route": route})
    return {"user_query": query, "language": detect_language(query), "errors": errors, "trace_events": events, "next_route": route}


def plan_request(state: CutoverAgentState) -> CutoverAgentState:
    events = state.get("trace_events", [])
    errors = state.get("errors", [])
    try:
        if state.get("fake_plan"):
            plan = CutoverAgentPlan.model_validate(state["fake_plan"])
            stats = {"hit": 0, "miss": 0}
        elif os.environ.get("CARVEOPS_FAKE_PLANNER") == "1":
            plan = heuristic_plan(state["user_query"])
            stats = {"hit": 0, "miss": 0}
        else:
            plan, stats, _digest = call_planner(
                state["user_query"],
                provider=state.get("provider", "deepseek"),
                model=state.get("model", DEFAULT_MODELS["deepseek"]),
                offline=state.get("offline", False),
            )
        plan = normalize_plan_for_query(state["user_query"], plan)
        requests = tool_requests_from_plan(plan)
        route = "clarification" if plan.needs_clarification else "continue"
        events.append({"node": "plan_request", "intent": plan.intent, "route": route, "cache": stats})
        return {
            "plan": plan.model_dump(),
            "planner_cache": stats,
            "tool_requests": [item.model_dump() for item in requests],
            "trace_events": events,
            "next_route": route,
        }
    except Exception as error:  # noqa: BLE001
        errors.append({"code": "PLANNER_FAILED", "message": str(error)})
        events.append({"node": "plan_request", "route": "error"})
        return {"errors": errors, "trace_events": events, "next_route": "error"}


def enforce_policy(state: CutoverAgentState) -> CutoverAgentState:
    events = state.get("trace_events", [])
    plan = state.get("plan", {})
    requests = list(state.get("tool_requests", []))
    allow_rebuild = state.get("allow_rebuild", False)
    denied_reason = ""
    validated: list[dict[str, Any]] = []
    if plan.get("intent") == "unsupported":
        requests = []
    if len(requests) > MAX_TOOL_CALLS:
        denied_reason = "Planner requested too many tools."
    for request in requests:
        name = request.get("tool_name")
        args = request.get("arguments", {}) or {}
        if denied_reason:
            break
        if name not in ALLOWED_TOOLS:
            denied_reason = f"Tool is not allowed: {name}"
            break
        extra = set(args) - TOOL_ARGUMENTS[name]
        if extra:
            denied_reason = f"Tool {name} received unsupported arguments: {sorted(extra)}"
            break
        if has_forbidden_path_value(args):
            denied_reason = f"Tool {name} contains a forbidden path-like or oversized argument."
            break
        if name == "list_cutover_activities":
            if args.get("status") is not None and args["status"] not in ACTIVITY_STATUSES:
                denied_reason = "Invalid activity status."
                break
        if name == "list_raid_items":
            if args.get("raid_type") is not None and args["raid_type"] not in RAID_TYPES:
                denied_reason = "Invalid RAID type."
                break
            if args.get("status") is not None and args["status"] not in RAID_STATUSES:
                denied_reason = "Invalid RAID status."
                break
            if args.get("severity") is not None and args["severity"] not in RAID_SEVERITIES:
                denied_reason = "Invalid RAID severity."
                break
        if name == "rebuild_cutover_reports":
            if not allow_rebuild:
                denied_reason = "Rebuild tools require --allow-rebuild."
                break
            query = state.get("user_query", "").lower()
            if args.get("rebuild_plan") and not any(term in query for term in ("plan", "计划", "重建计划", "rebuild plan")):
                denied_reason = "rebuild_plan=true requires an explicit plan rebuild request."
                break
            if not any(term in query for term in ("rebuild", "重新生成", "重建")):
                denied_reason = "A query request cannot be converted into rebuild."
                break
        validated.append({"tool_name": name, "arguments": args, "reason": request.get("reason", "")})
    allowed = not denied_reason
    events.append({"node": "enforce_policy", "allowed": allowed, "tool_count": len(validated)})
    return {
        "policy_decision": {
            "allowed": allowed,
            "denied_reason": denied_reason,
            "validated_tool_requests": validated if allowed else [],
        },
        "tool_requests": validated if allowed else [],
        "trace_events": events,
        "next_route": "continue" if allowed else "error",
    }


def extract_payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for content in getattr(result, "content", []):
        text = getattr(content, "text", None)
        if not text:
            continue
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("MCP tool result did not contain JSON.")


async def execute_mcp_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not requests:
        return []
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", SERVER_MODULE],
        cwd=str(PROJECT_ROOT),
    )
    results: list[dict[str, Any]] = []
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            for request in requests:
                name = request["tool_name"]
                args = request.get("arguments", {})
                payload = extract_payload(await session.call_tool(name, args))
                result = {
                    "tool_name": name,
                    "arguments": args,
                    "ok": payload.get("ok") is True,
                    "data": payload.get("data") if payload.get("ok") is True else None,
                    "error": payload.get("error") if payload.get("ok") is not True else None,
                    "source_content_sha256": None,
                }
                data = result["data"] or {}
                if isinstance(data, dict):
                    result["source_content_sha256"] = data.get("source_content_sha256") or data.get("daily_content_sha256") or data.get("status_content_sha256")
                results.append(result)
    return results


def execute_mcp_tools(state: CutoverAgentState) -> CutoverAgentState:
    events = state.get("trace_events", [])
    requests = state.get("tool_requests", [])
    try:
        results = asyncio.run(execute_mcp_requests(requests))
        success_count = sum(1 for item in results if item["ok"])
        route = "continue" if success_count or not requests else "error"
        events.append({"node": "execute_mcp_tools", "mcp_session": 1 if requests else 0, "success_count": success_count})
        return {"tool_results": results, "mcp_sessions": 1 if requests else 0, "trace_events": events, "next_route": route}
    except Exception as error:  # noqa: BLE001
        errors = state.get("errors", [])
        errors.append({"code": "MCP_FAILED", "message": str(error)})
        events.append({"node": "execute_mcp_tools", "route": "error"})
        return {"errors": errors, "tool_results": [], "mcp_sessions": 1 if requests else 0, "trace_events": events, "next_route": "error"}


def first_result(results: list[dict[str, Any]], tool_name: str) -> dict[str, Any] | None:
    for result in results:
        if result["tool_name"] == tool_name and result["ok"]:
            return result
    return None


def cite(results: list[dict[str, Any]]) -> list[dict[str, str]]:
    citations = []
    for result in results:
        sha = result.get("source_content_sha256")
        if result.get("ok") and sha:
            citations.append({"tool": result["tool_name"], "sha": sha})
    return citations


def format_citations(citations: list[dict[str, str]], language: str) -> list[str]:
    if not citations:
        return []
    if language == "zh":
        lines = ["数据来源："]
        lines.extend(f"- {item['tool']} · SHA {item['sha']}" for item in citations)
    else:
        lines = ["Data sources:"]
        lines.extend(f"- {item['tool']} · SHA {item['sha']}" for item in citations)
    return lines


def compose_unsupported(query: str, language: str) -> str:
    if language == "zh":
        return (
            "这个请求超出了当前 Cutover Copilot 的只读边界。\n\n"
            "当前 Agent 只能通过 MCP 查询计划、状态、日报、活动和 RAID，并可在显式授权时重建报告。"
            "它不能修改活动状态，也不能写入 cutover_status_updates.json。"
        )
    return (
        "This request is outside the current read-only Cutover Copilot boundary.\n\n"
        "The agent can query plan, status, daily brief, activities, and RAID through MCP, "
        "and can rebuild reports only when explicitly allowed. It cannot modify activity status "
        "or write cutover_status_updates.json."
    )


def compose_from_results(state: CutoverAgentState) -> tuple[str, list[dict[str, str]]]:
    language = state.get("language", "en")
    plan = state.get("plan", {})
    intent = plan.get("intent")
    results = state.get("tool_results", [])
    citations = cite(results)
    if intent == "unsupported":
        return compose_unsupported(state.get("user_query", ""), language), []

    daily = first_result(results, "get_cutover_daily_brief")
    status = first_result(results, "get_cutover_status_summary")
    activities = first_result(results, "list_cutover_activities")
    raid = first_result(results, "list_raid_items")
    rebuild = first_result(results, "rebuild_cutover_reports")

    lines: list[str] = []
    if language == "zh":
        if rebuild:
            data = rebuild["data"]
            lines.append(f"已重建 Cutover 报告，validation = {data['validation']}。")
            lines.append("这只重建确定性报告，没有写入状态事件。")
            lines.append(f"当前 Overall RAG 为 {data['overall_rag']}。")
        elif intent == "raid_review" and raid:
            items = raid["data"]["raid_items"]
            lines.append(f"当前共有 {len(items)} 个 Risk RAID 项。")
            for idx, item in enumerate(items, 1):
                lines.append(f"{idx}. {item['raid_id']}：{item['current_status']}，severity={item['severity']}，owner={item['owner_role']}。")
        elif intent in {"blocked_activities", "activity_search"} and activities:
            rows = activities["data"]["activities"]
            lines.append(f"当前有 {len(rows)} 个匹配活动。")
            for idx, item in enumerate(rows, 1):
                blocker = item.get("blocker") or "无 blocker 文本"
                lines.append(f"{idx}. {item['activity_id']}：{item['current_status']}，owner={item['owner_role']}，blocker={blocker}")
            if daily:
                gate = daily["data"]["headline"].get("next_gate") or {}
                lines.append(f"下一审批门是 {gate.get('gate_id')}，当前为 {gate.get('current_status')}。")
        elif intent == "management_actions" and daily:
            actions = daily["data"]["management_actions"]
            lines.append(f"当前管理层动作共有 {len(actions)} 项。")
            for idx, action in enumerate(actions, 1):
                lines.append(f"{idx}. {action['source_id']}：{action['action']}")
        elif intent == "daily_brief" and daily:
            data = daily["data"]
            lines.append(f"当前 Cutover 状态为 {data['overall_rag']}（截至 {data['as_of_offset']}）。")
            lines.append(f"T-7 到期未完成活动 {len(data['due_now'])} 个，下一批到期活动 {len(data['due_next'])} 个。")
            for item in data["due_now"]:
                lines.append(f"- {item['activity_id']}：{item['current_status']}，owner={item['owner_role']}")
        else:
            if daily:
                head = daily["data"]["headline"]
                lines.append(f"当前 Cutover 状态为 {daily['data']['overall_rag']}（截至 {head['as_of_offset']}）。")
                lines.append(f"30 个活动中，{head['completed_activity_count']} 个已完成，{head['blocked_activity_count']} 个被阻塞，{head['not_started_activity_count']} 个尚未开始。")
                gate = head.get("next_gate") or {}
                lines.append(f"下一审批门是 {gate.get('gate_id')}，当前为 {gate.get('current_status')}。")
            elif status:
                counts = status["data"]["activity_status_counts"]
                lines.append(f"当前活动状态：Completed {counts['Completed']}，Blocked {counts['Blocked']}，Not Started {counts['Not Started']}。")
        lines.extend(format_citations(citations, language))
    else:
        if rebuild:
            data = rebuild["data"]
            lines.append(f"Cutover reports were rebuilt with validation = {data['validation']}.")
            lines.append("This only rebuilt deterministic reports; no status events were written.")
            lines.append(f"The current Overall RAG is {data['overall_rag']}.")
        elif intent == "raid_review" and raid:
            items = raid["data"]["raid_items"]
            lines.append(f"There are {len(items)} Risk RAID items.")
            for idx, item in enumerate(items, 1):
                lines.append(f"{idx}. {item['raid_id']}: {item['current_status']}, severity={item['severity']}, owner={item['owner_role']}.")
        elif intent in {"blocked_activities", "activity_search"} and activities:
            rows = activities["data"]["activities"]
            lines.append(f"There are {len(rows)} matching activities.")
            for idx, item in enumerate(rows, 1):
                blocker = item.get("blocker") or "no blocker text"
                lines.append(f"{idx}. {item['activity_id']}: {item['current_status']}, owner={item['owner_role']}, blocker={blocker}")
            if daily:
                gate = daily["data"]["headline"].get("next_gate") or {}
                lines.append(f"The next gate is {gate.get('gate_id')}, currently {gate.get('current_status')}.")
        elif intent == "management_actions" and daily:
            actions = daily["data"]["management_actions"]
            lines.append(f"There are {len(actions)} management actions.")
            for idx, action in enumerate(actions, 1):
                lines.append(f"{idx}. {action['source_id']}: {action['action']}")
        elif intent == "daily_brief" and daily:
            data = daily["data"]
            lines.append(f"The current Cutover status is {data['overall_rag']} as of {data['as_of_offset']}.")
            lines.append(f"{len(data['due_now'])} incomplete activities are due now and {len(data['due_next'])} are due next.")
            for item in data["due_now"]:
                lines.append(f"- {item['activity_id']}: {item['current_status']}, owner={item['owner_role']}")
        else:
            if daily:
                head = daily["data"]["headline"]
                lines.append(f"The current Cutover status is {daily['data']['overall_rag']} as of {head['as_of_offset']}.")
                lines.append(f"Across 30 activities, {head['completed_activity_count']} are completed, {head['blocked_activity_count']} are blocked, and {head['not_started_activity_count']} are not started.")
                gate = head.get("next_gate") or {}
                lines.append(f"The next gate is {gate.get('gate_id')}, currently {gate.get('current_status')}.")
            elif status:
                counts = status["data"]["activity_status_counts"]
                lines.append(f"Activity status: Completed {counts['Completed']}, Blocked {counts['Blocked']}, Not Started {counts['Not Started']}.")
        lines.extend(format_citations(citations, language))
    if not lines:
        lines = ["未能生成回答。" if language == "zh" else "Unable to compose an answer."]
    return "\n".join(lines), citations


def compose_answer(state: CutoverAgentState) -> CutoverAgentState:
    events = state.get("trace_events", [])
    errors = state.get("errors", [])
    if errors or (state.get("policy_decision") and not state["policy_decision"].get("allowed")):
        if state.get("plan", {}).get("intent") == "unsupported":
            answer, citations = compose_from_results(state)
        else:
            reason = errors[-1]["message"] if errors else state["policy_decision"].get("denied_reason")
            if state.get("language") == "zh":
                answer = f"无法完成该请求：{reason}"
            else:
                answer = f"Unable to complete the request: {reason}"
            citations = []
    elif state.get("plan", {}).get("needs_clarification"):
        question = state["plan"].get("clarification_question") or "Please clarify the request."
        answer = question if state.get("language") == "en" else f"需要澄清：{question}"
        citations = []
    else:
        answer, citations = compose_from_results(state)
    events.append({"node": "compose_answer", "citations": len(citations)})
    return {"final_answer": answer, "citations": citations, "trace_events": events}


def validate_answer(state: CutoverAgentState) -> CutoverAgentState:
    answer = state.get("final_answer", "")
    valid = bool(answer.strip())
    reasons: list[str] = []
    forbidden = ("reasoning_content", "Traceback", "Authorization", "Bearer")
    if any(item in answer for item in forbidden):
        valid = False
        reasons.append("forbidden text")
    if re.search(r"[A-Za-z]:\\", answer):
        valid = False
        reasons.append("absolute path")
    called = {item["tool_name"]: item for item in state.get("tool_results", []) if item.get("ok")}
    for citation in state.get("citations", []):
        result = called.get(citation["tool"])
        if not result or result.get("source_content_sha256") != citation["sha"]:
            valid = False
            reasons.append("citation mismatch")
    if not valid:
        language = state.get("language", "en")
        answer = "输出验证失败，已停止返回不安全内容。" if language == "zh" else "Output validation failed; unsafe content was not returned."
    events = state.get("trace_events", [])
    events.append({"node": "validate_output", "valid": valid, "reasons": reasons})
    return {"final_answer": answer, "validation": {"valid": valid, "reasons": reasons}, "trace_events": events}


def route_after_validate(state: CutoverAgentState) -> str:
    return "compose_answer" if state.get("next_route") == "error" else "plan_request"


def route_after_plan(state: CutoverAgentState) -> str:
    return "compose_answer" if state.get("next_route") in {"error", "clarification"} else "enforce_policy"


def route_after_policy(state: CutoverAgentState) -> str:
    return "compose_answer" if state.get("next_route") == "error" else "execute_mcp_tools"


def route_after_execute(state: CutoverAgentState) -> str:
    return "compose_answer"


def build_graph():
    graph = StateGraph(CutoverAgentState)
    graph.add_node("validate_input", validate_input)
    graph.add_node("plan_request", plan_request)
    graph.add_node("enforce_policy", enforce_policy)
    graph.add_node("execute_mcp_tools", execute_mcp_tools)
    graph.add_node("compose_answer", compose_answer)
    graph.add_node("validate_output", validate_answer)
    graph.add_edge(START, "validate_input")
    graph.add_conditional_edges("validate_input", route_after_validate, {"plan_request": "plan_request", "compose_answer": "compose_answer"})
    graph.add_conditional_edges("plan_request", route_after_plan, {"enforce_policy": "enforce_policy", "compose_answer": "compose_answer"})
    graph.add_conditional_edges("enforce_policy", route_after_policy, {"execute_mcp_tools": "execute_mcp_tools", "compose_answer": "compose_answer"})
    graph.add_conditional_edges("execute_mcp_tools", route_after_execute, {"compose_answer": "compose_answer"})
    graph.add_edge("compose_answer", "validate_output")
    graph.add_edge("validate_output", END)
    return graph.compile()


def stable_write_report(payload: dict[str, Any], path: Path, runtime_info: dict[str, Any] | None = None) -> dict[str, Any]:
    report = attach_run_info(payload)
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            old_run = existing.get("_run_info", {})
            if old_run.get("content_sha256") == report["_run_info"].get("content_sha256") and old_run.get("generated_at"):
                report["_run_info"]["generated_at"] = old_run["generated_at"]
        except json.JSONDecodeError:
            pass
    if runtime_info:
        report["_run_info"].update(runtime_info)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def trace_payload(state: CutoverAgentState) -> dict[str, Any]:
    return {
        "_meta": {
            "component": "cutover_langgraph_agent",
            "graph": GRAPH_NAME,
            "provider": state.get("provider"),
            "model": state.get("model"),
            "allow_rebuild": state.get("allow_rebuild"),
        },
        "request": {
            "request_id": state.get("request_id"),
            "user_query": state.get("user_query"),
        },
        "plan": state.get("plan", {}),
        "policy": state.get("policy_decision", {}),
        "tool_calls": state.get("tool_results", []),
        "final_answer": state.get("final_answer", ""),
        "validation": state.get("validation", {}),
        "mcp_sessions": state.get("mcp_sessions", 0),
        "graph_path": [event.get("node") for event in state.get("trace_events", [])],
        "errors": state.get("errors", []),
    }


def run_agent(
    query: str,
    *,
    provider: str = "deepseek",
    model: str | None = None,
    offline: bool = False,
    allow_rebuild: bool = False,
    trace_output: Path = DEFAULT_TRACE_PATH,
    fake_plan: dict[str, Any] | None = None,
) -> tuple[CutoverAgentState, dict[str, Any]]:
    model = model or DEFAULT_MODELS[provider]
    graph = build_graph()
    initial: CutoverAgentState = {
        "request_id": stable_request_id(query),
        "user_query": query,
        "provider": provider,
        "model": model,
        "offline": offline,
        "allow_rebuild": allow_rebuild,
        "trace_output": str(trace_output),
        "errors": [],
        "trace_events": [],
        "planner_cache": {"hit": 0, "miss": 0},
        "mcp_sessions": 0,
    }
    if fake_plan is not None:
        initial["fake_plan"] = fake_plan
    final_state = graph.invoke(initial)
    trace = stable_write_report(
        trace_payload(final_state),
        trace_output,
        runtime_info={
            "offline": offline,
            "planner_cache": final_state.get("planner_cache", {"hit": 0, "miss": 0}),
            "trace_events": final_state.get("trace_events", []),
        },
    )
    return final_state, trace


def run_formal_queries(
    *,
    offline: bool,
    allow_rebuild: bool = False,
    provider: str = "deepseek",
    model: str | None = None,
) -> list[dict[str, Any]]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for query in FORMAL_QUERIES:
        state, trace = run_agent(
            query,
            provider=provider,
            model=model or DEFAULT_MODELS[provider],
            offline=offline,
            allow_rebuild=allow_rebuild,
            trace_output=query_run_path(query),
        )
        results.append({"query": query, "state": state, "trace": trace})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Cutover LangGraph Agent.")
    parser.add_argument("--query", required=True)
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--model", default=None)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--allow-rebuild", action="store_true")
    parser.add_argument("--trace-output", default=str(DEFAULT_TRACE_PATH))
    args = parser.parse_args()
    state, trace = run_agent(
        args.query,
        provider=args.provider,
        model=args.model,
        offline=args.offline,
        allow_rebuild=args.allow_rebuild,
        trace_output=Path(args.trace_output),
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(state["final_answer"])
    print(f"\nTrace SHA: {trace['_run_info']['content_sha256']}")
    print(f"Planner cache: {state.get('planner_cache', {}).get('hit', 0)} hit / {state.get('planner_cache', {}).get('miss', 0)} miss")
    return 0 if state.get("validation", {}).get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
