# Finals Script — Top 5, 3:45–4:15 PM

**Format:** present to the **whole room** — a **3-minute live demo** + **one judge question**.
A timed performance, not a conversation. Over time = cut off before your close = you lose.
**Target 2:40, leave 20s slack.**

**Judged on:** Approach & technical merit · Insight & problem understanding · Communication &
clarity · Creativity. **Win condition:** the room feels one wow moment (map lights up) and
remembers one idea (**4D: climb-or-wait beats going around — 64% less fuel**).

---

## Pre-flight (the second you're named a finalist) — commands in DEMO_RUNBOOK.md
- [ ] Laptop plugged in, screen mirrored, font/zoom large
- [ ] App running + map loaded at `http://localhost:8000/`, warm-up question already answered
- [ ] `demo_inject` armed in a terminal
- [ ] **Backup video cued to fullscreen** — pivot in 2 seconds if wifi/app dies
- [ ] Decide who drives, who talks

---

## The 3-minute script (timed beats)

### 0:00–0:25 — Stakes (DON'T touch the laptop; look at the room)
> "Turbulence is the number one cause of injuries in commercial aviation — 36% of accidents
> over the last 15 years — and it costs U.S. airlines up to half a billion dollars a year.
> Air Space Intelligence's Flyways already saves fuel at that scale — 1.2 million gallons for
> Alaska. We built the conversational, multi-agent 4D layer that sits on top of it. It's FlightStrain."

### 0:25–0:45 — What it is (turn to the map)
> "A live 4D model of the airspace, built on the data you gave us. Underneath: eight specialist
> agents — weather, traffic, conflict, wind, safety — each watching a slice and reporting to one
> coordinator. The dispatcher doesn't read a dashboard. They talk to it."

### 0:45–1:25 — Beat 1: conversational + agent-driven map
*[type: "any flights heading into weather near the corridor?"]*
> "Plain-English question..." *[answer streams; camera flies; flights highlight]* "...plain-English
> answer, real callsigns, and it drives the map itself. I didn't move that camera — the agent did.
> This is a day of US flight plans — ~15,000 flights — with real HRRR weather."

### 1:25–2:15 — Beat 2: THE WOW + THE INSIGHT (hit demo_inject)
*[trigger demo_inject — map lights red]*
> "A pilot reports turbulence inside a real forecast cell. The system corroborates it, projects
> every aircraft forward, and flags the fleet flying into it — by callsign, with ETAs."
*[pause — let the map land, then deliver the insight]*
> "But here's what nobody ships. It doesn't just say 'go around.' It reads the storm in 4D — width,
> height, *and time* — and finds that of the 85 flights heading into this weather, most can **climb
> over it or wait a few minutes for it to pass.** That's **64% less fuel** than a conventional 2D
> reroute, and 13,500 passengers spared a rough ride. **Time is an escape route 2D tools can't see.**"

### 2:15–2:40 — Close on the idea + roadmap (look back at the room)
> "Today it's one Python process; every agent is intentionally kagent-shaped — production is five
> Kubernetes services, about a week out. We're not reinventing the forecast — we own the decision
> on top of it. We think this is what airspace operations looks like in 2027, and we want to build
> it with you."

*[Stop. Hands off the keyboard. Let it land.]*

---

## The judge question (you get one — answer in 2–3 sentences, then STOP)

- **"How is this different from Flyways / what's novel?"**
  > "Flyways optimizes a route a dispatcher reads. We make it conversational and multi-agent — they
  > talk to it, and it auto-correlates the hazard against their live fleet. And the insight: Flyways
  > reroutes; we show in 4D most flights don't need to — they climb over or wait. We're not claiming
  > the forecast — NOAA's GTGN fuses that — we own the decision last mile."

- **"Is the data real?"**
  > "Yes — your scenario flight plans plus real NOAA/HRRR weather. The only synthetic element is the
  > single corroborating pilot report in the trigger; the storm and the traffic are real."

- **"Why is this interesting / what's the insight?"** *(a criterion — hit it hard)*
  > "Every turbulence tool treats avoidance as going around — 2D. But turbulence is a 4D volume. Once
  > you model time, the cheapest escape is usually to climb over or wait minutes, not divert. We
  > quantified it on your data: 64% less fuel. That reframes the problem."

- **"How does the multi-agent system work?"**
  > "Eight Claude-backed specialists publish findings to a shared bus; the coordinator synthesizes
  > them and surfaces only what crosses an urgency threshold — so it can speak first, unprompted."

- **"What broke / what's not done?"**
  > "Honest: the conflict-geometry and ML risk scoring are wired but we'd harden them on more scenario
  > data. The demo path you saw is fully real and runs offline."

---

## Performance rules
- **Rehearse twice out loud before 5pm**, on the real laptop, real clicks. Non-negotiable.
- Open looking at the **room**, not the screen.
- The demo_inject + 4D-insight is the climax — slow down, let the map work, don't talk over it.
- If anything flakes: "let me show you the recorded run" → backup video. Never debug on stage.
- **End on the idea (climb-or-wait, 64%), out loud, while the map's up.** Then stop.
- Running long? Cut Beat 1 — never cut the close.
