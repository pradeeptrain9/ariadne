# Demo video — run of show

Target **2:50**, hard cap 3:00. Screen recording with voiceover; no slides, no intro card beyond
one title frame. Judges watch a lot of these, so the first fifteen seconds have to contain the
idea.

**The scenario.** An alert pages on `catalog-service`. It arrives the way a real one would — an
Alertmanager webhook POSTed to Ariadne — and Ariadne answers the three questions on-call asks
first: what changed, what else breaks, who owns it. It is investigated once with memory withheld
and once with memory available, so the cost of remembering is visible on screen. Then the same
graph is queried from Claude Code over MCP, the way an engineer would use it mid-incident.

**What makes it live, not a slideshow.** Three things happen for real on camera: an HTTP POST in
Alertmanager's webhook format lands and the page picks it up; the investigation is a real model
call whose tool calls, tokens and cost stream as they happen; and the MCP question is answered
from the same Cypher. The data is synthetic and seeded, and the video says so once, plainly.

**This costs money to record.** Each live investigation is a real API call. Measured on
`claude-sonnet-5` over 20 alerts: warm $0.0274, cold $0.0438 on average. A take runs one cold
and one warm, so ≈ **$0.07** — call it $0.30 with a rehearsal and three takes. Firing the
webhook, the MCP shot and re-seeding cost nothing.

## Before recording (about ten minutes)

```bash
docker compose up -d
.venv/bin/python -m ariadne.seed          # deterministic: same graph every take
.venv/bin/python -m ariadne.verify        # must end "all checks passed"
mv runs.jsonl runs.old.jsonl              # if present — see below
.venv/bin/python -m ariadne.server
```

- **Bars.** With no local `runs.jsonl` the page reads the committed 20-alert
  `docs/measurements/runs.jsonl` and says so on the caption line. A local `runs.jsonl` wins over
  it, so move any old one aside first, or the bars show whatever was last run on this machine.
- **Rehearse once, then re-seed.** Do one full take off-camera — webhook, cold, warm, MCP — then
  run `ariadne.seed` again. Every investigation writes back to the graph, and a re-seed is what
  makes the next take start from the same world. If a take fails, re-seed before the next.
- **Browser** at 1440×900, dark mode, zoom 100%, bookmarks bar hidden. Three tabs:
  1. `http://localhost:8000` — the demo page.
  2. https://blog.cloudflare.com/18-november-2025-outage, scrolled to the timeline table.
  3. `http://localhost:3000` — FalkorDB Browser, with the shot 5 query already pasted in.
- **Terminal**, font size up, placed so it and the page's *Investigate* card are both on screen
  for shot 2 (a split screen, or the terminal across the bottom third). Working directory
  `ariadne/`, with the shot 2 command typed and not yet run.
- **Claude Code** open in `ariadne/` for shot 6. `.mcp.json` registers the Ariadne server, so
  the tools are there without setup; confirm with `/mcp` before recording.
- Do Not Disturb on.

## Real-world references

Every outside claim in the script has a source, and the source is on screen when it is said.

| Claim | Source |
|---|---|
| "Roughly 70% of outages are due to changes in a live system" | Google SRE book, introduction — https://sre.google/sre-book/introduction/ |
| Cloudflare, 18 Nov 2025: permissions change deployed 11:05, impact 11:28, investigation first chased Workers KV and a suspected DDoS, focus moved to the Bot Management file at 13:37 | Cloudflare postmortem — https://blog.cloudflare.com/18-november-2025-outage |
| Recovery from a bad deploy is a measured industry metric ("failed deployment recovery time") | DORA — https://dora.dev/guides/dora-metrics/ |
| Alertmanager's webhook body (version 4: `status`, `alerts[].labels`, `startsAt`, `fingerprint`) | Prometheus docs — https://prometheus.io/docs/alerting/latest/configuration/#webhook_config |
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

Each shot lists what is **on screen**, what you **do**, and what you **say**. Read every number
off the screen at that moment — live runs vary, and the figures quoted here are only what to
expect.

### 1 · 0:00–0:25 — the problem, from a real outage

**On screen:** Cloudflare postmortem tab, title and URL visible. After the first paragraph, cut
to the demo page: the full mesh, nothing running.

**Say:**

> "Last November, Cloudflare's worst outage in six years started with a database permissions
> change at 11:05. The team spent the next two hours chasing a downstream service and a
> suspected DDoS. Google's SRE book says roughly seventy percent of outages come from changes.
> The change was in the graph the whole time."

*(cut to the page)*

> "Ariadne answers the three questions on-call asks first: what changed, what else breaks, who
> owns it. This is a graph of forty services, their deploys, their commits, and the people who
> own them. Synthetic and seeded. Nothing here is pre-computed."

**Do:** trace the three tiers with the cursor — customer-facing, services, infrastructure. Do
not click.

### 2 · 0:25–0:40 — the alert arrives

**On screen:** terminal and the *Investigate* card side by side.

**Do:** run the prepared command.

```bash
.venv/bin/python -m ariadne.fire --print | curl -s -H 'Content-Type: application/json' \
  --data @- http://127.0.0.1:8000/api/alertmanager | python3 -m json.tool
```

Within three seconds the page shows the red **Alertmanager webhook** banner, selects the new
alert in the picker, and lights `catalog-service` red on the mesh.

**Say:**

> "This is Alertmanager's webhook format — the same JSON Prometheus sends any receiver. It just
> paged on catalog-service. Ariadne filed it as a node in the graph, and before anything is
> investigated it has already computed the alert's structural signature."

Point at `signature` in the curl response. Do not explain it yet — shot 4 pays it off.

*Honest footnote, if asked or if there is time:* `ariadne.fire` places `startsAt` on the seeded
graph's clock, a few minutes after the service's last deploy, because the synthetic deploys are
not on today's date. A real Alertmanager's wall-clock `startsAt` is accepted too; against this
graph its window simply holds no deploys.

### 3 · 0:40–1:20 — a cold investigation

**Do:** tick **withhold memory**, press **Investigate**.

**Say:**

> "I'm withholding memory, so the agent has to work from scratch."

**On screen:** tool calls stream into *Live run* — expect about seven — with the meters
climbing. Point at each as it lands:

> "It walks up the dependency graph to what catalog-service relies on, finds what shipped in the
> window, and ranks commits by how much they touched this service's own code — that overlap is
> computed by the graph, not judged by the model. Then it measures the blast radius and finds the
> owner."

As the mesh lights, read the count off the mesh note:

> "*N* services break when catalog-service breaks. That's a traversal, not an estimate. Red is
> what breaks with it; blue dashed is where it searched for the cause — two traversals answering
> two different questions."

As the verdict lands, point at the **computed card above the prose** — this is the shot that
makes the design rule visible rather than asserted:

> "This card is not the model's answer. It's read back out of the graph the agent just wrote
> to: the culprit commit, the chain from alert to service to deploy to commit, and the
> confidence — with the three numbers that produced it. The model passed its own estimate too.
> It's stored next to this one and used for nothing."

If the prose names a tie or a shared-library commit, say so — it is the honesty beat:

> "And it says what it did *not* establish: when commits tie on overlap, it refuses to pick."

**End** on the meters, read aloud: "*seven* tool calls, about *five* cents."

### 3b · 1:20–1:30 — the same walk, on the real outage

**On screen:** the Cloudflare tab's timeline, or the mapping table above as an overlay.

> "On Cloudflare's outage, the same walk goes from Workers KV up through the proxy to the
> ClickHouse change at 11:05, and the blast radius is the list in their postmortem. One honest
> catch: that change was two hops upstream, so file overlap scores it zero. Ariadne surfaces it
> and flags it as a cross-service cause — it does not rank it first. That blind spot is
> recorded in its memory."

### 4 · 1:30–1:55 — the same alert, with memory

**Do:** untick **withhold memory**, press **Investigate** on the same alert.

> "Same alert, same graph. This time it can remember."

**On screen:** the first log line is the memory hit. Read its counts off the screen:

> "That signature matched. In *N* of *N* past incidents with this shape, the cause was a commit
> in a deploy to the failing service itself. That's computed from the graph — not the model
> remembering. So there's one thing left to check."

Expect four tool calls. As it finishes, point at the meters, then the *Cold vs warm* bars:

> "Four tool calls instead of seven. About half the cost. The same culprit commit and the same
> confidence, because the same three numbers are in the graph either way. Memory changed what it
> cost to find the answer, not what the answer is. Over twenty alerts that's 1.6 times cheaper,
> with the same culprit every time."

The 1.6× is on the bars' caption when the committed ledger is loaded; check it is before saying
it. Pause a beat on the bars.

### 5 · 1:55–2:10 — the honest part

**On screen:** the bars, then the FalkorDB Browser tab; run the pasted query.

```cypher
MATCH (old:Plan)-[r:SUPERSEDED_BY]->(new:Plan)
MATCH (t:Tool)-[f:FAILS_ON]->(c:Condition)
RETURN old.id, old.runs, old.successes, r.reason, t.name, c.name
```

> "This didn't work the first time. The first version stored transcripts, and replaying a
> transcript costs what deriving it costs — we measured 1.1×. What fixed it was storing the
> shape of the answer. And the memory stores failures too: this plan was wrong twice, so it was
> retired, and the graph records which tool misled it."

### 6 · 2:10–2:30 — how an engineer uses it mid-incident

**On screen:** Claude Code in the terminal.

**Do:** type, and let it run:

> catalog-service is paging. Using the ariadne tools: what breaks if it's down, who's on call
> for it, and what's the runbook?

**Say**, as it calls `blast_radius`, `owner_of` and `runbook_for`:

> "Ariadne is also an MCP server, read-only by default. Mid-incident, the engineer asks in plain
> English from Claude Code, and the answer comes back from the same graph queries the agent
> uses."

Keep the question to impact, owner and runbook. The MCP tools have no alert lookup, so asking
"what changed in the last hour" makes the client hunt for a timestamp on camera. Rehearse this
prompt; it runs on your Claude Code session, not the project's API key.

### 7 · 2:30–2:50 — close

**On screen:** back to the page, mesh lit, bars visible.

> "Agents that act on connected data, and agent memory — one graph doing both. Alerts come in
> through Alertmanager's webhook today. The data is synthetic and seeded, so you can clone it and
> get this exact demo, and the schema is shaped for the rest — deploys from GitHub, owners and
> dependencies from a service catalog. That's the next thing to build."

## If a take runs long

Cut in this order: the FalkorDB query in shot 5, then the second sentence of shot 6, then shot
2's footnote (it is already optional). Never cut the computed card or the refusal in shot 3, the
overlap-0 catch in shot 3b, or the bars in shot 4 — those are the things judges remember.

## Do not say

- "AI-powered" anything.
- That the agent "decides" what caused the incident. It does not; the graph ranks and the model
  explains. Saying otherwise invites exactly the question the design answers.
- That the model "scores" or "estimates" the confidence. It passes an estimate that is stored
  and ignored; the number on screen is `scoring.confidence` over three graph facts.
- Any number not visible on screen at that moment.
- That Ariadne "would have caught" or "would have prevented" the Cloudflare outage. It would have
  walked to the change and flagged it; it would not have ranked it first, and it prevents nothing.
- That it runs on a real company's data. Alerts arrive through a real webhook format; the
  services, deploys and commits behind them are synthetic.
- That the webhook starts the investigation. It files the alert; a person presses Investigate,
  because each investigation is a paid model call.
