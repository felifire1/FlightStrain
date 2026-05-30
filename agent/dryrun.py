"""Dry-run harness for the agent loop.

Validates every tool without calling the Anthropic API:
    - JSON-schema sanity on each tool definition
    - Executes each tool against live data with realistic args
    - Prints what the messages payload to Claude would look like

Run:
    .venv/bin/python -m agent.dryrun
"""
from __future__ import annotations
import json
import sys

from agent.tools import TOOLS, EXECUTORS, run_tool

REQUIRED_TOOL_FIELDS = {"name", "description", "input_schema"}

SAMPLE_ARGS = {
    "get_metar": {"icao": "KBOS"},
    "get_taf": {"icao": "KBOS"},
    "get_sigmets": {},
    "get_turbulence_advisories": {},
    "get_traffic": {"lamin": 42.0, "lamax": 42.7, "lomin": -71.5, "lomax": -70.7},
}


def check_schema(tool: dict) -> list[str]:
    errs = []
    missing = REQUIRED_TOOL_FIELDS - set(tool)
    if missing:
        errs.append(f"missing fields: {missing}")
    schema = tool.get("input_schema", {})
    if schema.get("type") != "object":
        errs.append("input_schema.type must be 'object'")
    if "properties" not in schema:
        errs.append("input_schema missing 'properties'")
    if tool["name"] not in EXECUTORS:
        errs.append(f"no executor registered for {tool['name']}")
    return errs


def truncate(s: str, n: int = 300) -> str:
    return s if len(s) <= n else s[:n] + f"... <{len(s)-n} more chars>"


def main() -> int:
    print(f"loaded {len(TOOLS)} tools\n")
    all_ok = True

    for tool in TOOLS:
        name = tool["name"]
        errs = check_schema(tool)
        status = "OK" if not errs else "FAIL"
        if errs:
            all_ok = False
        print(f"[{status}] {name}")
        for e in errs:
            print(f"    schema: {e}")

        args = SAMPLE_ARGS.get(name)
        if args is None:
            print(f"    skip exec: no SAMPLE_ARGS for {name}")
            continue

        print(f"    exec args: {json.dumps(args)}")
        result = run_tool(name, args)
        # try to summarize
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict) and "error" in parsed:
                print(f"    -> ERROR: {parsed['error']}")
                all_ok = False
            elif isinstance(parsed, dict):
                keys = list(parsed.keys())[:8]
                print(f"    -> dict, keys={keys}")
            elif isinstance(parsed, list):
                print(f"    -> list, n={len(parsed)}")
            else:
                print(f"    -> {type(parsed).__name__}")
            print(f"    raw: {truncate(result)}")
        except Exception as e:
            print(f"    -> non-JSON result: {e}")
        print()

    # what the Claude payload would look like
    print("=" * 60)
    print("payload Claude would receive:")
    print("=" * 60)
    print(json.dumps({
        "model": "claude-sonnet-4-6",
        "system": "(see agent/loop.py)",
        "tools": [{"name": t["name"], "input_schema_keys": list(t["input_schema"].get("properties", {}))} for t in TOOLS],
        "messages": [{"role": "user", "content": "What's the weather at Boston Logan and is it good for flying?"}],
    }, indent=2))

    print()
    print("RESULT:", "all green" if all_ok else "ISSUES — see above")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
