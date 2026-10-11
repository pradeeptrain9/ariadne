# Demo video — shot list

Target **2:30**, hard cap 3:00. Screen recording with voiceover; no slides, no intro card beyond
one title frame. Judges watch a lot of these, so the first fifteen seconds have to contain the
idea.

**This costs money to record.** Each live investigation is a real API call: warm ≈ $0.07, cold
≈ $0.13. The script below runs three (one warm, one cold, one novel-shape) ≈ **$0.30**, plus
retakes. Budget ~$1 if you expect three takes.

## Before recording

```bash
docker compose up -d
.venv/bin/python -m ariadne.seed          # deterministic: same graph every take
.venv/bin/python -m ariadne.verify        # all green before you hit record
.venv/bin/python -m ariadne.server
```

- Keep `runs.jsonl` from a previous `replay --n 3` so the cold/warm bars are already populated
  when the page loads. An empty chart in shot 1 wastes the payoff.
- Browser at 1440×900, dark mode, zoom 100%. Hide bookmarks bar.
- Second terminal ready for shot 5, font size up.
- Close anything that can produce a notification.

## Shots

**1 · 0:00–0:20 — the problem, over the mesh**

Page loaded, nothing running.

> "An alert fires. The on-call engineer spends forty minutes answering three questions the
> company already knows the answers to: what changed, what else breaks, who owns it. This is a
> graph of forty services, their deploys, their commits, and the people who own them. Nothing
> here is pre-computed."

Cursor traces the three tiers. Do not click yet.

**2 · 0:20–1:05 — a cold investigation**

Select **A1 repeated shape — catalog-service**, tick **withhold memory**, press Investigate.

> "I'm withholding memory, so the agent has to work from scratch."

Let the tool calls stream. Point at them as they land.

> "It narrows the dependency cone, finds what shipped in the window, ranks commits by how much
> they touched the failing service's own code — that overlap is computed by the graph, not
> judged by the model — then measures the blast radius and finds the owner."

As the mesh lights:

> "Nine services break when catalog-service breaks. Six of them are customer-facing. That's a
> traversal, not an estimate."

On the verdict, land on the honesty beat — this is the moment that separates the entry:

> "And it says what it did *not* establish: two commits tie on overlap, so it refuses to pick
> between them, and it flags a shared-library commit that its own ranking scores low."

End shot on the counter: **8 tool calls, $0.13**.

**3 · 1:05–1:40 — the same alert, with memory**

Untick **withhold memory**. Press Investigate again.

> "Same alert, same graph. This time the agent can remember."

Point at the memory-hit line as it appears:

> "Thirteen of thirteen past incidents with this shape were caused by a commit in a deploy to the
> failing service itself. That hypothesis is computed from the graph — it's not the model
> recalling something."

> "So there's one thing left to check. Four tool calls instead of eight. Half the cost. Same
> culprit commit — and higher confidence, because the history agrees with the window."

Let the cold/warm bars update on screen. Pause a beat on them.

**4 · 1:40–2:05 — the honest part**

Stay on the bars.

> "This did not work the first time. The first version stored each plan as a transcript of the
> investigation that produced it, and replaying a transcript costs what deriving it costs. We
> measured 1.1× — warm runs actually burned *more* tokens than cold. A memory that replays
> everything saves nothing."

> "What fixed it was storing the *shape of the answer* instead of the steps. Memory turns an
> open-ended search into a single hypothesis test."

**5 · 2:05–2:25 — it's a real graph, and real tools**

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

**6 · 2:25–2:40 — close**

Back to the page.

> "Agents that act on connected data, and agent memory — one graph doing both. Everything is
> synthetic and seeded, so you can clone it and get this exact demo."

## If a take runs long

Cut shot 5's FalkorDB browser query first, then shot 4's second paragraph. Never cut the
honesty beat in shot 2 or the bars in shot 3 — those are the two things judges remember.

## Do not say

- "AI-powered" anything.
- That the agent "decides" what caused the incident. It does not; the graph ranks and the model
  explains. Saying otherwise invites exactly the question the design answers.
- Any number not visible on screen at that moment.
