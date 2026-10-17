# Demo video — shot list

Target **2:45**, hard cap 3:00. Screen recording with voiceover; no slides, no intro card beyond
one title frame. Judges watch a lot of these, so the first fifteen seconds have to contain the
idea.

**This costs money to record.** Each live investigation is a real API call. Measured on
`claude-sonnet-5` over 20 alerts: warm $0.0274, cold $0.0438 on average. The script below
runs one cold and one warm, so ≈ **$0.07** a take — call it $0.25 with a rehearsal and three
takes.

## Before recording

```bash
docker compose up -d
.venv/bin/python -m ariadne.seed          # deterministic: same graph every take
.venv/bin/python -m ariadne.verify        # all green before you hit record
.venv/bin/python -m ariadne.server
```

- The cold/warm bars are populated on load either way: with no local `runs.jsonl` the page reads
  the committed 20-alert `docs/measurements/runs.jsonl` and says so on the caption line. A local
  `runs.jsonl` wins over it, so move any old one aside (`mv runs.jsonl runs.old.jsonl`) to show
  the 20-alert bars.
- Browser at 1440×900, dark mode, zoom 100%. Hide bookmarks bar.
- Second terminal ready for shot 5, font size up.
- Close anything that can produce a notification.
- Browser tab open on the Cloudflare postmortem, scrolled to the timeline table:
  https://blog.cloudflare.com/18-november-2025-outage — shot 1 shows its title and URL, so the
  numbers in the opener are on screen when they are said.

## Real-world references

Every outside claim in the script has a source, and the source is on screen when it is said.

| Claim | Source |
|---|---|
| "Roughly 70% of outages are due to changes in a live system" | Google SRE book, introduction — https://sre.google/sre-book/introduction/ |
| Cloudflare, 18 Nov 2025: permissions change deployed 11:05, impact 11:28, investigation first chased Workers KV and a suspected DDoS, focus moved to the Bot Management file at 13:37 | Cloudflare postmortem — https://blog.cloudflare.com/18-november-2025-outage |
| Recovery from a bad deploy is a measured industry metric ("failed deployment recovery time") | DORA — https://dora.dev/guides/dora-metrics/ |
| Counter-example: AWS us-east-1, Oct 2025, latent race condition in DynamoDB DNS management, not a recent change | AWS post-event summaries — https://aws.amazon.com/premiumsupport/technology/pes/ |

How the Cloudflare incident maps onto Ariadne's graph — and where it would not:

| Cloudflare | Ariadne |
|---|---|
| Workers KV errors | `Alert -[:FIRED_ON]-> Service` |
| KV → core proxy → Bot Management → ClickHouse | `upstream_candidates`, `DEPENDS_ON` up to 3 hops |
| 11:05 permissions change | `deploys_in_window` |
| CDN, Turnstile, Workers KV, Access, Dashboard down | `blast_radius` |
| Trigger was two hops upstream of the alerting service | `suspect_commits` scores it overlap 0 — surfaced and flagged as a cross-service cause, **not** ranked first. That is the recorded blind spot, `FAILS_ON shared_library_change`. |

Say the last row on camera. It is the honest version, and it is the one a judge who reads the
postmortem would check.

## Shots

**1 · 0:00–0:25 — the problem, from a real outage**

Open on the Cloudflare postmortem tab, title and URL visible, for the first sentence only.

> "Last November, Cloudflare's worst outage in six years started with a database permissions
> change at 11:05. The team spent the next two hours chasing a downstream service and a
> suspected DDoS. Google's SRE book says roughly seventy percent of outages come from changes.
> The change was in the graph the whole time."

Cut to the page, nothing running.

> "Ariadne answers the three questions on-call asks first: what changed, what else breaks, who
> owns it. This is a graph of forty services, their deploys, their commits, and the people who
> own them. Synthetic and seeded. Nothing here is pre-computed."

Cursor traces the three tiers. Do not click yet. The run legend (failing / blast radius /
dependency cone) is hidden until a run starts, so there is nothing on screen yet to explain.

**2 · 0:25–1:10 — a cold investigation**

Select **A1 repeated shape — catalog-service**, tick **withhold memory**, press Investigate.

> "I'm withholding memory, so the agent has to work from scratch."

Let the tool calls stream. Point at them as they land.

> "It narrows the dependency cone, finds what shipped in the window, ranks commits by how much
> they touched the failing service's own code — that overlap is computed by the graph, not
> judged by the model — then measures the blast radius and finds the owner."

As the mesh lights:

> "Nine services break when catalog-service breaks. Six of them are customer-facing. That's a
> traversal, not an estimate."

As the verdict lands, point at the **computed card above the prose** — this is the shot that
makes the design rule visible rather than asserted:

> "This card is not the model's answer. It is read back out of the graph the agent just wrote
> to: the culprit, the chain from alert to service to deploy to commit, and the confidence —
> with the three numbers that produced it. The model passed its own estimate too. It is stored
> next to this one and used for nothing."

Two colours on the mesh are worth naming while the card is up:

> "Blue dashed is what it searched for a cause — the dependency cone. Red is what breaks with
> it. Those are two different traversals answering two different questions."

Then the honesty beat — this is the moment that separates the entry:

> "And it says what it did *not* establish: two commits tie on overlap, so it refuses to pick
> between them, and it flags a shared-library commit that its own ranking scores low."

End shot on the counter: **7 tool calls, $0.05**.

**2b · 1:10–1:20 — the same walk, on the real outage**

Overlay the Cloudflare → Ariadne mapping table from *Real-world references* above.

> "On Cloudflare's outage, the same walk goes from Workers KV up through the proxy to the
> ClickHouse change at 11:05, and the blast radius is the list in their postmortem. One honest
> catch: that change was two hops upstream, so file overlap scores it zero. Ariadne surfaces it
> and flags it as a cross-service cause — it does not rank it first. That blind spot is
> recorded in its memory."

**3 · 1:20–1:50 — the same alert, with memory**

Untick **withhold memory**. Press Investigate again.

> "Same alert, same graph. This time the agent can remember."

Point at the memory-hit line as it appears:

> "Nine of nine past incidents with this shape were caused by a commit in a deploy to the
> failing service itself. That hypothesis is computed from the graph — it's not the model
> recalling something."

> "So there's one thing left to check. Four tool calls instead of seven. Half the cost. Same
> culprit commit — and the same computed confidence, because the same three numbers are in the
> graph either way. Memory changed what it cost to find the answer, not what the answer is."

Let the cold/warm bars update on screen. Pause a beat on them.

**4 · 1:50–2:10 — the honest part**

Stay on the bars.

> "This did not work the first time. The first version stored each plan as a transcript of the
> investigation that produced it, and replaying a transcript costs what deriving it costs. We
> measured 1.1× — warm runs actually burned *more* tokens than cold. A memory that replays
> everything saves nothing."

> "What fixed it was storing the *shape of the answer* instead of the steps. Memory turns an
> open-ended search into a single hypothesis test."

**5 · 2:10–2:25 — it's a real graph, and real tools**

Cut to the terminal.

```bash
.venv/bin/python -m ariadne.replay --report-only
```

> "Every number comes out of a ledger, not a slide."

Then, briefly, the FalkorDB browser at `localhost:3000` — run one query live:

```cypher
MATCH (old:Plan)-[r:SUPERSEDED_BY]->(new:Plan)
MATCH (t:Tool)-[f:FAILS_ON]->(c:Condition)
RETURN old.id, old.runs, old.successes, r.reason, t.name, c.name
```

> "The memory stores failures too. This plan was wrong twice, so it was retired, and the graph
> records which tool misled it and why."

**6 · 2:25–2:45 — close**

Back to the page.

> "Agents that act on connected data, and agent memory — one graph doing both. Everything is
> synthetic and seeded, so you can clone it and get this exact demo. The schema is shaped for
> real sources — alerts from PagerDuty or Alertmanager, deploys and commits from GitHub,
> dependencies and owners from a service catalog — and that ingestion is the next thing to build."

## If a take runs long

Cut shot 5's FalkorDB browser query first, then shot 4's second paragraph. Never cut the
honesty beat in shot 2, the overlap-0 catch in shot 2b, or the bars in shot 3 — those are the
things judges remember.

## Do not say

- "AI-powered" anything.
- That the agent "decides" what caused the incident. It does not; the graph ranks and the model
  explains. Saying otherwise invites exactly the question the design answers.
- That the model "scores" or "estimates" the confidence. It passes an estimate that is stored
  and ignored; the number on screen is `scoring.confidence` over three graph facts.
- Any number not visible on screen at that moment.
- That Ariadne "would have caught" or "would have prevented" the Cloudflare outage. It would have
  walked to the change and flagged it; it would not have ranked it first, and it prevents nothing.
- That it runs on real alerts today. Ingestion is not built; the data is synthetic.
