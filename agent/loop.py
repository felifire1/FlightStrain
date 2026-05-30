"""Minimal Claude tool-use loop.

Run:
    .venv/bin/python -m agent.loop "What's the weather at Boston right now and is it safe to fly?"

Requires ANTHROPIC_API_KEY in env or .env.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from anthropic import Anthropic  # noqa: E402
from agent.tools import TOOLS, run_tool, MAP_ACTIONS  # noqa: E402


def _clean_block(b) -> dict:
    """The SDK's model_dump on a response content block emits output-only
    fields like `parsed_output`, `citations`, `caller` that the API rejects
    when sent back as input. Strip to just the canonical shape per type."""
    d = b if isinstance(b, dict) else b.model_dump()
    t = d.get("type")
    if t == "text":
        return {"type": "text", "text": d.get("text", "")}
    if t == "tool_use":
        return {"type": "tool_use", "id": d["id"], "name": d["name"], "input": d.get("input") or {}}
    if t == "tool_result":
        return {"type": "tool_result", "tool_use_id": d["tool_use_id"], "content": d.get("content")}
    # default: pass through (e.g. thinking blocks — currently unused)
    return d

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
SYSTEM = """You are a dispatcher / air traffic controller advising on the
Northeast National Airspace System. You sit alongside a 4D Cesium digital-twin
and can read weather / traffic / turbulence data via tools, and drive the map
via show_on_map.

VOICE: Controller cadence. Terse, declarative, factual. No marketing language.
No exclamations. No emojis under any circumstance. No bold/italic for
emphasis — the data IS the emphasis. Numbers and identifiers carry the message:
callsigns, registrations, advisory IDs, altitudes in feet/FL, distances in NM,
times in Zulu. Use standard phraseology where it fits: "FL280", "moderate
turb, surface to 8000", "240 at 15", "12 NM south", "advisory active". Never
recap the user's question. No preamble. Do not say "Sure" or "Of course".

LENGTH: 1-2 sentences for status/number questions. 3 sentences max otherwise.
Headline first. Plain prose, not bullets or headers, unless the user asks
for a list.

LATENCY: each tool call adds ~1s. Minimum set. Don't load layers the user
didn't ask for. Don't call multiple weather tools when one will do.

show_on_map for visual changes only:
- "show me / where / highlight" → yes
- A number or status → no
- When naming ONE specific flight, you may highlight that one
- Do not pre-load layers unless asked

Available layers and what each shows:
- traffic   — live aircraft tracks (overnight replay)
- decisions — red glowing trails of auditor-flagged flights
- turb      — G-AIRMET turbulence advisory polygons (forecast)
- pirep     — pilot reports as 3D points colored by turb intensity (ground truth)
- sigmet    — red convective-storm SIGMET polygons (extruded by FL top)
- winds     — FL450 wind arrows; magenta = jet stream >70 kt
- radar     — NEXRAD base reflectivity (surface-level precip)
- airports  — 3D airport markers
- buildings — OSM 3D buildings (BOS only, on close zoom)

If the user asks about *turbulence*, prefer pirep over turb for "what's actually
happening". turb is forecast, pirep is observed. SIGMETs are convection (storms),
not turb.

Technical vocabulary welcome: TRACON, ARTCC, separation minima, advisory
polygon, decision moment, 4D trajectory, G-AIRMET, SIGMET, PIREP, FL, agl.
ASI product references (PRESCIENCE, Flyways) are appropriate when discussing
optimization or what an integration would have recommended."""


def chat(user_question: str, history: list[dict] | None = None, max_turns: int = 6) -> dict:
    """Run one user turn. Returns {text, map_actions, tool_trace, history}.
    `history` is the prior conversation in Anthropic message format; pass it back
    on the next call to maintain multi-turn context."""
    client = Anthropic()
    messages = list(history or []) + [{"role": "user", "content": user_question}]
    MAP_ACTIONS.clear()
    tool_trace: list[dict] = []

    for turn in range(max_turns):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=400,  # ~3 sentences; system prompt caps this
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "tool_use":
            assistant_blocks = []
            tool_results = []
            for block in resp.content:
                assistant_blocks.append(_clean_block(block))
                if block.type == "tool_use":
                    args = block.input or {}
                    print(f"  [tool] {block.name}({json.dumps(args)})", file=sys.stderr)
                    result = run_tool(block.name, args)
                    tool_trace.append({"name": block.name, "args": args})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "assistant", "content": assistant_blocks})
            messages.append({"role": "user", "content": tool_results})
            continue

        text = "".join(b.text for b in resp.content if b.type == "text")
        messages.append({"role": "assistant", "content": [_clean_block(b) for b in resp.content]})
        return {
            "text": text,
            "map_actions": list(MAP_ACTIONS),
            "tool_trace": tool_trace,
            "history": messages,
        }

    return {
        "text": "[max turns reached without final answer]",
        "map_actions": list(MAP_ACTIONS),
        "tool_trace": tool_trace,
        "history": messages,
    }


def chat_stream(user_question: str, history: list[dict] | None = None, max_turns: int = 6):
    """Generator: yields {type, ...} events for streaming to the UI.

    Event types:
      - {"type":"text_delta","delta":"..."}              — incremental assistant text
      - {"type":"tool_use","name":"...","args":{...}}    — a tool just ran
      - {"type":"map_action","cmd":{...}}                — UI command to execute
      - {"type":"done","history":[...],"tool_trace":[...]}  — end of turn
    """
    client = Anthropic()
    messages = list(history or []) + [{"role": "user", "content": user_question}]
    MAP_ACTIONS.clear()
    tool_trace: list[dict] = []
    map_emitted = 0

    for _turn in range(max_turns):
        with client.messages.stream(
            model=MODEL,
            max_tokens=400,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            for event in stream:
                # 'text' events fire for each token-ish chunk of assistant text.
                # tool_use blocks come through as their own block stream and we
                # only need the final accumulated args; pull them from the final
                # message rather than re-parse input_json deltas here.
                if getattr(event, "type", None) == "text":
                    yield {"type": "text_delta", "delta": event.text}
            final = stream.get_final_message()

        if final.stop_reason == "tool_use":
            assistant_blocks = []
            tool_results = []
            for block in final.content:
                assistant_blocks.append(_clean_block(block))
                if block.type == "tool_use":
                    args = block.input or {}
                    print(f"  [tool] {block.name}({json.dumps(args)})", file=sys.stderr)
                    tool_trace.append({"name": block.name, "args": args})
                    yield {"type": "tool_use", "name": block.name, "args": args}
                    result = run_tool(block.name, args)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
                    # Drain any new map actions that show_on_map just queued.
                    while len(MAP_ACTIONS) > map_emitted:
                        yield {"type": "map_action", "cmd": MAP_ACTIONS[map_emitted]}
                        map_emitted += 1
            messages.append({"role": "assistant", "content": assistant_blocks})
            messages.append({"role": "user", "content": tool_results})
            continue

        # end_turn
        messages.append({"role": "assistant", "content": [_clean_block(b) for b in final.content]})
        yield {"type": "done", "history": messages, "tool_trace": tool_trace}
        return

    yield {"type": "done", "history": messages, "tool_trace": tool_trace,
           "warning": "max turns reached"}


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What's the current weather at Boston Logan and is it good for flying?"
    result = chat(q)
    print(result["text"])
    if result["map_actions"]:
        print("\n[map_actions]", json.dumps(result["map_actions"], indent=2), file=sys.stderr)
