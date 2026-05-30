"""Interactive CLI for the multi-agent specialists.

Loads real cached data (latest snapshots from the overnight recorder),
synthesizes events, runs them through every specialist, prints color-coded
findings, then drops into a REPL where you can ask the Coordinator questions.

Usage:
    .venv/bin/python scripts/specialists_demo.py

Once at the prompt:
    > whats the biggest risk right now
    > fire emergency      # inject a synthetic 7700 squawk
    > scenario weather    # replay the latest weather feed
    > scenario traffic    # replay the latest traffic snapshot
    > scenario all        # replay everything
    > findings            # list everything currently on the bus
    > help
    > quit
"""
from __future__ import annotations
import glob
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.specialists.base import Event
from agent.specialists.bus import bus
from agent.specialists.coordinator import Coordinator
from agent.specialists.weather import WeatherAgent
from agent.specialists.traffic import TrafficAgent
from agent.specialists.safety import SafetyAgent
from agent.specialists.fleet import FleetAgent
from agent.specialists.narrator import NarratorAgent


# ANSI colors so findings are scannable in the terminal
DIM, RESET = "\033[2m", "\033[0m"
SEV_COLOR = {
    0: "\033[90m",   # gray
    1: "\033[36m",   # cyan
    2: "\033[33m",   # yellow
    3: "\033[35m",   # magenta
    4: "\033[91m",   # bright red
    5: "\033[101m\033[97m",  # red bg + white fg
}
SPEC_TAG = {
    "weather":      "\033[94m",   # blue
    "traffic":      "\033[92m",   # green
    "safety":       "\033[91m",   # red
    "fleet":        "\033[95m",   # magenta
    "narrator":     "\033[96m",   # cyan
    "coordinator":  "\033[97m",   # white-bright
}


def render(finding) -> str:
    spec = finding.specialist.split(".")[0]
    sev_tag = f"{SEV_COLOR.get(finding.severity, '')} S{finding.severity} {RESET}"
    spec_tag = f"{SPEC_TAG.get(spec, '')}[{spec.upper():^11}]{RESET}"
    out = f"{sev_tag} {spec_tag} {finding.summary}"
    if finding.recommended_action:
        out += f"\n            {DIM}→ {finding.recommended_action}{RESET}"
    if finding.map_actions:
        out += f"\n            {DIM}map: {finding.map_actions}{RESET}"
    return out


# ----- Data loaders: build Events from cached recorder output ---------------

def latest_file(pattern: str) -> Path | None:
    matches = sorted(glob.glob(str(ROOT / pattern)))
    return Path(matches[-1]) if matches else None


def build_weather_events() -> list[Event]:
    events = []
    # G-AIRMET snapshot
    f = latest_file("data/overnight/weather/gairmet_*.json")
    if f:
        adv = json.loads(f.read_text())
        events.append(Event(
            type="gairmet.snapshot",
            payload={"advisories": adv, "source_file": f.name},
        ))
    # SIGMETs
    f = latest_file("data/overnight/weather/airsigmet_*.json")
    if f:
        all_sig = json.loads(f.read_text())
        for s in all_sig:
            if s.get("hazard") == "CONVECTIVE":
                events.append(Event(
                    type="sigmet.issued",
                    payload={
                        "hazard": "CONVECTIVE",
                        "id": s.get("id", "?"),
                        "states": " ".join(s.get("rawAirSigmet", "")
                                            .split("\n")[3:4]).strip() or "n/a",
                        "tops_ft": 38000,
                        "duration_min": 120,
                    },
                ))
                break  # just one for demo brevity
    # PIREPs
    f = ROOT / "data/samples/pireps_us.json"
    if f.exists():
        all_p = json.loads(f.read_text())
        reports = []
        for p in all_p[:200]:
            ints = [(p.get(f"tbInt{n}") or "").strip().upper() for n in (1, 2)]
            ints = [i for i in ints if i]
            worst = max(ints, default="", key=lambda x: ("NEG","SMTH","LGT","MOD","SEV","EXTM").index(x.split("-")[0]) if x and x.split("-")[0] in ("NEG","SMTH","LGT","MOD","SEV","EXTM") else 0)
            reports.append({"lat": p.get("lat"), "lon": p.get("lon"), "worst_intensity": worst})
        events.append(Event(type="pirep.snapshot", payload={"reports": reports}))
    return events


def build_traffic_event() -> Event | None:
    f = latest_file("data/overnight/traffic/traffic_*.jsonl")
    if not f:
        return None
    last = None
    with f.open() as fh:
        for line in fh:
            if line.strip():
                last = json.loads(line)
    if not last:
        return None
    fields = ["icao24","callsign","origin_country","time_position","last_contact",
              "lon","lat","baro_alt","on_ground","velocity","heading","vert_rate",
              "sensors","geo_alt","squawk","spi","position_source"]
    states = [dict(zip(fields, s)) for s in (last.get("states") or [])]
    return Event(type="traffic.snapshot", payload={"states": states})


def fake_emergency() -> Event:
    return Event(type="traffic.snapshot", payload={"states": [
        {"icao24": "a0e250", "callsign": "AAL1767", "baro_alt": 4500,
         "velocity": 130, "on_ground": False, "squawk": "7700",
         "vert_rate": -18.0},
    ]})


# ----- The REPL --------------------------------------------------------------

HELP = """
commands:
  <question>             ask the coordinator (any free text)
  scenario weather       replay latest weather feeds
  scenario traffic       replay latest traffic snapshot
  scenario all           replay both
  fire emergency         inject a synthetic 7700 squawk
  findings               list everything currently on the bus
  voices                 show what each specialist would say to "any chop?"
  help                   this
  quit | exit | q        leave
"""


def main():
    specs = [WeatherAgent(), TrafficAgent(), SafetyAgent(), FleetAgent(), NarratorAgent()]
    coord = Coordinator(specialists=specs)

    print(f"\n{SPEC_TAG['coordinator']}=== Multi-agent ATC ops room ==={RESET}")
    print(f"{DIM}{len(specs)} specialists loaded. Mode: stub (no LLM calls).")
    print(f"Type 'help' for commands, or just ask a question.{RESET}\n")

    def fire(events: list[Event]):
        for ev in events:
            for s in specs:
                if ev.type in s.interests():
                    for f in s.formulate(ev):
                        bus.publish(f)
                        print(render(f))

    while True:
        try:
            line = input("\n\033[97m>\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        if line in ("quit", "exit", "q"):
            break
        if line == "help":
            print(HELP); continue
        if line == "findings":
            recent = bus.latest(20)
            if not recent:
                print(f"{DIM}(bus is empty){RESET}")
            for f in recent:
                print(render(f))
            continue
        if line == "voices":
            print(f"{DIM}what each specialist would emit for that question (no synthesis):{RESET}")
            test_ev = Event(type="user.question", payload={"text": "any chop?"})
            for s in specs:
                if "user.question" in s.interests():
                    for f in s.formulate(test_ev):
                        print(render(f))
            continue
        if line.startswith("scenario"):
            which = line[len("scenario"):].strip() or "all"
            events = []
            if which in ("weather", "all"):
                events += build_weather_events()
            if which in ("traffic", "all"):
                e = build_traffic_event()
                if e: events.append(e)
            print(f"{DIM}firing {len(events)} events...{RESET}")
            fire(events); continue
        if line == "fire emergency":
            fire([fake_emergency()]); continue

        # Treat anything else as a user question to the coordinator
        result = coord.handle_user(line)
        print()
        print(f"{SPEC_TAG['coordinator']}┌── COORDINATOR ──┐{RESET}")
        for ln in result["text"].splitlines():
            print(f"  {ln}")
        if result["map_actions"]:
            print(f"\n  {DIM}map_actions: {result['map_actions']}{RESET}")
        if result["voices"]:
            print(f"\n  {DIM}voices: {[v['specialist'] for v in result['voices']]}{RESET}")


if __name__ == "__main__":
    main()
