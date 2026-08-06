"""Smoke test the local cutover MCP server over stdio.

The test launches:
    python -m src.mcp_servers.cutover_server

It uses the official MCP Python client and calls only deterministic local
tools. No network, LLM, SAP, shell, or arbitrary path access is involved.
"""

from __future__ import annotations

import asyncio
import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "get_cutover_plan_summary",
    "get_cutover_status_summary",
    "get_cutover_daily_brief",
    "list_cutover_activities",
    "list_raid_items",
    "rebuild_cutover_reports",
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
    raise RuntimeError("Tool result did not contain a JSON object.")


def require_ok(payload: dict[str, Any], tool_name: str) -> dict[str, Any]:
    if payload.get("ok") is not True:
        raise RuntimeError(f"{tool_name} failed: {payload}")
    return payload["data"]


async def run_smoke(*, allow_write_rebuild: bool = False) -> None:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.mcp_servers.cutover_server"],
        cwd=str(PROJECT_ROOT),
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tool_names = {tool.name for tool in tools_result.tools}
            missing = EXPECTED_TOOLS - tool_names
            if missing:
                raise RuntimeError(f"Missing expected MCP tools: {sorted(missing)}")

            plan = require_ok(
                extract_payload(await session.call_tool("get_cutover_plan_summary", {})),
                "get_cutover_plan_summary",
            )
            status = require_ok(
                extract_payload(await session.call_tool("get_cutover_status_summary", {})),
                "get_cutover_status_summary",
            )
            daily = require_ok(
                extract_payload(await session.call_tool("get_cutover_daily_brief", {})),
                "get_cutover_daily_brief",
            )
            blocked = require_ok(
                extract_payload(await session.call_tool("list_cutover_activities", {"status": "Blocked"})),
                "list_cutover_activities",
            )
            risks = require_ok(
                extract_payload(await session.call_tool("list_raid_items", {"raid_type": "Risk"})),
                "list_raid_items",
            )
            rebuild = None
            if allow_write_rebuild:
                rebuild = require_ok(
                    extract_payload(await session.call_tool("rebuild_cutover_reports", {"rebuild_plan": False})),
                    "rebuild_cutover_reports",
                )

    print("MCP server: carveops-cutover")
    print(f"Tools discovered: {len(tool_names)}")
    print(f"Plan activities: {plan['activity_count']}")
    print(f"Events applied: {status['events_applied_count']}")
    print(f"Overall RAG: {daily['overall_rag']}")
    print(f"Blocked activities: {blocked['count']}")
    print(f"Risk items: {risks['count']}")
    print(f"Rebuild validation: {rebuild['validation'] if rebuild else 'skipped'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test the local cutover MCP server over stdio.")
    parser.add_argument(
        "--allow-write-rebuild",
        action="store_true",
        help="Also call rebuild_cutover_reports, which writes formal cutover artifacts by default.",
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(run_smoke(allow_write_rebuild=args.allow_write_rebuild))
    except Exception as error:  # noqa: BLE001 - smoke test should print a concise failure.
        print(f"MCP smoke test failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
