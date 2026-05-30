# Table Showcase — 3:00–3:30 PM (90 sec, repeatable)

**Format:** judges rotate table-to-table; you demo in place, leaning over the laptop,
2–5 min, interruptible. You run this **many times** — make it smooth, vary nothing.

**Judged on:** Approach & technical merit · Insight & problem understanding · Communication
& clarity · Creativity. (Not business/TAM — so lead with the *insight*, not a scary number.)

**The one sentence they repeat to other judges:**
> "That's the team whose airspace you *talk to* — it reads the weather in 4D and finds that
> most flights heading into a storm can climb over it or wait it out, not burn fuel going around."

---

## Pre-flight (before judges arrive) — exact commands in DEMO_RUNBOOK.md
- [ ] App up + map loaded at `http://localhost:8000/` (never make them watch it boot)
- [ ] One warm-up question already answered in the chat (no blank panel)
- [ ] `demo_inject` armed in a terminal tab
- [ ] **Report open** (`data/reports/turbulence_avoidance_*.md`) — your number, on paper
- [ ] Backup video in another tab; font/zoom up for 3 readers

---

## The 90-second core (built / data / why — what they asked for)

**[Hook — 15s]**
> "Turbulence is the #1 cause of injuries in commercial aviation — about a third of all
> airline accidents — and costs carriers up to half a billion dollars a year. ASI's Flyways
> already saves fuel at scale. We built the conversational, 4D decision layer on top of it."

**[What we built — 25s, on the map]**
> "This is a live 4D model of the airspace. Behind it, eight specialist agents — weather,
> traffic, conflict, wind, safety — feed one coordinator the dispatcher *talks to*."
*[type: "any flights heading into weather?"]*
> "Plain-English question, plain-English answer — and it drives the map itself. I never
> touched the camera; the agent did."

**[The data — 10s]**
> "Straight off the challenge data you gave us — a day of US flight plans, real HRRR weather,
> real airspace. Nothing mocked but the one pilot report I inject to trigger the demo."

**[The wow — visual — 15s — hit demo_inject]**
> "A pilot reports turbulence inside a real forecast cell. Instantly the agent projects every
> aircraft forward and flags the ones flying into it — by callsign, with ETAs."
*[map lights up red — let the screen show the count; don't quote it]*

**[THE INSIGHT — the number to remember — 20s]**
> "Here's the part nobody ships. It doesn't just say 'go around.' It reads the storm in 4D —
> width, height, *and time* — and finds that of 85 flights heading into this weather, most can
> **climb over it or briefly wait for it to pass**. That cuts avoidance fuel **64%** versus a
> normal 2D reroute and spares **13,500 passengers** a rough ride. **Time is an escape route
> 2D tools can't see.**"

**[Close — 5s]**
> "One Python process today; every agent is kagent-shaped — production is about a week out."

---

## Q&A bank (they WILL probe — answers ≤ 2 sentences)

**"Is this real data or mocked?"**
> "Real — your scenario flight plans plus real NOAA/HRRR weather. The only synthetic piece is
> the single pilot report that triggers the demo; the storm and the traffic are real."

**"How is this different from Flyways / ASI?"** *(the key one)*
> "Flyways optimizes a route a dispatcher *reads*; we make it conversational and multi-agent —
> they *talk* to it and it auto-correlates the hazard against their live fleet. And the insight:
> Flyways reroutes; we show that in 4D most flights don't need to — they climb over or wait. We're
> not reinventing the forecast — NOAA's GTGN already fuses that — we own the *decision*."

**"What's the AI actually doing?"** *(they handed out tokens)*
> "Eight Claude-backed specialists each watch a slice of the data and publish findings; the
> coordinator synthesizes them into one dispatcher-voice answer and decides what's urgent enough
> to surface unprompted."

**"Why is this interesting / what's the insight?"** *(maps straight to a criterion)*
> "Every turbulence tool treats avoidance as 2D — go around. But turbulence is a 4D volume; once
> you model time, the cheapest escape is usually to climb over or wait minutes. We quantified it:
> 64% less fuel. That reframes the problem."

**"What's not done / more time?"**
> "The conflict-geometry and ML risk agents are wired but we'd harden them on more scenario data.
> Next: live SWIM ingest, kagent on Kubernetes, eVTOL deconfliction for Joby."

---

## Table rules
- **Always be mid-demo when a judge walks up** — never a blank screen.
- If it breaks: "let me show you the recorded run" → backup video. Never debug live.
- Let them interrupt; answer, then return to **the 4D-insight line** — that's what they remember.
- Land on **64% / climb-or-wait**, not the alarm count. Working + specific + one sharp insight wins.
