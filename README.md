# Ariadne

An on-call agent whose context *and* memory are a graph.

An alert fires. Ariadne answers the three questions a human spends forty minutes on — **what
changed, what else breaks, who owns it** — and shows the path through a FalkorDB graph that got
it there. Then it remembers: the resolved incident is written back as a reusable plan, so the
next alert with the same *structural shape* replays that plan instead of re-investigating.

Built for [Graph Hacks](https://www.wemakedevs.org/hackathons/falkordb) (FalkorDB, Oct 15–18
2026). Entering two tracks: **Agents That Act on Connected Data** and **Agent Memory and
Coordination**.

## Status

| | |
|---|---|
| Graph, schema, deterministic seed, verification | done |
| Nine graph tools, confidence scoring, cost ledger | done |
| Agent loop (tool runner, caching, effort policy) | written; **unrun** — needs an API key |
| Cold-vs-warm measurement harness | written; unrun |
| MCP server (stdio, read-only by default) | done |
| Demo page with live streaming run | done |
| Submission write-up, measurements, video shot list | done |
| Demo video | not recorded |

Everything below is measured, not projected — 56 tests, and the numbers come out of
`runs.jsonl`.

**Timeline disclosure:** the hackathon window is Oct 15–18. The design notes in `docs/` and the
graph/seed/verification code in `ariadne/` were written on **Oct 11**, before the window opened —
earlier than rule 8 intends. Recorded here rather than disguised. Commits carry their real dates.

**AI assistance disclosure (rule 11):** written with Claude Code (Claude Opus 5) — design notes,
the seeder, the test suite. The FalkorDB behaviours in `docs/decisions.md` were each found by
running queries against a live instance, not taken from the model's or the documentation's word.

## The design rule

**Ranking is computed, never asked of a model.** Cypher traversal and FalkorDB's graph algorithms
decide which commit is suspect and what the blast radius is. Claude decides which tool to call
and writes the explanation. Remove the model and the verdict is unchanged — it just reads worse.
Remove the graph and there is no product (rule 3).

A model asked *which commit broke this* answers fluently and sometimes wrongly, and nothing
downstream can tell the difference, because a right answer and a wrong one are both well-formed
prose.

## Quick start

```bash
docker compose up -d                      # FalkorDB 6.0.2, pinned by digest
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m ariadne.seed          # deterministic graph + fixtures/scenarios.json
.venv/bin/python -m ariadne.verify        # graph correctness, prints every number it found
.venv/bin/python -m pytest -q             # 56 tests, no API key needed
```

Verified from a clean clone on a fresh virtualenv, in that order. The install also puts
`ariadne-seed`, `ariadne-verify`, `ariadne-serve`, `ariadne-agent`, `ariadne-replay` and
`ariadne-mcp` on the path if you prefer those.

The agent itself needs one credential — `ANTHROPIC_API_KEY`, and nothing else:

```bash
cp .env.example .env                      # then edit .env
.venv/bin/python -m ariadne.agent --scenario a1
```

`.env` is gitignored and must stay that way: this repo is public. A shell export works too, and
takes precedence over the file. Never put the key in `.env.example`, `.mcp.json`, or anything
else git tracks.

The FalkorDB Browser is at <http://localhost:3000> if you want to poke at the graph by hand.

## The same tools over MCP

`tools.py` is the only definition of the tools; the MCP server reflects over it, and a test
asserts the two surfaces agree on names, parameters and guidance. Verified against a real stdio
client: 8 read-only tools exposed, `blast_radius("orders-db")` returning 15 impacted services
including all six tier-1 APIs.

Writes are off by default — `record_outcome` mutates the memory graph, so it is only exposed with
`ARIADNE_MCP_ALLOW_WRITES=1`.

```bash
.venv/bin/python -m ariadne.mcp_server        # stdio
```

[.mcp.json](.mcp.json) wires it into Claude Code from the project root. For Claude Desktop, use
the same block with absolute paths in `claude_desktop_config.json`.

## What the memory is worth

Measured, `claude-opus-5`, same alerts in both arms:

| | tool calls | tokens | cost | wall |
|---|---|---|---|---|
| cold (memory withheld) | 8.0 | 36,017 | $0.1281 | 33.0s |
| warm (memory available) | 4.0 | 25,261 | $0.0681 | 18.9s |

**1.9× cheaper, 2× fewer tool calls, same culprit commit on every alert.** Warm runs also
reported *higher* confidence (0.72–0.80 vs 0.45–0.55), because the recalled hypothesis
corroborates what the current window shows.

The comparison is the same alerts against the same graph, not a warm run on one alert versus a
cold run on another — that would measure the alerts. `replay.py` asserts the warm arm reached the
same culprit as the cold arm; a cheap wrong answer is worse than an expensive right one.

Honest caveat: **the first design of this measured 1.1×, not 1.9×.** The plan stored a transcript
of the whole investigation, so replaying it was the same work — memory that replays everything
saves nothing. What fixed it was making `recall_plan` return a *hypothesis* computed from the
graph ("in 13 of 13 prior incidents of this shape, the cause was a commit in a deploy that
touched the failing service itself") bundled with the current impact, owner and runbook. A hit
then leaves exactly one thing to establish, and the warm path is 4 calls instead of 8. The
failure and the fix are both in `docs/walkthrough.md`.

Prompt caching is verified rather than assumed — `--check-cache` writes 3,214 tokens on the first
call and reads all 3,214 back on the second.

## Demo

```bash
.venv/bin/python -m ariadne.server        # http://127.0.0.1:8000
```

Needs `ANTHROPIC_API_KEY` — the page drives the real agent.

Pick an alert, press Investigate. Tool calls stream in over SSE as they happen, the blast radius
lights up on the mesh, the cost counter moves, and the cold-vs-warm bars update from the ledger
when the run finishes. "Withhold memory" forces the cold path on the same alert, which is the
A/B in one click.

Standard library only — no web framework. The demo has to run on a judge's machine from a clone
and a `docker compose up`.

## Graph model

Two subgraphs meeting at `Incident`: the **infrastructure** the agent reasons over, and the
**procedural memory** it writes back to. Full property tables in
[docs/graph-model.md](docs/graph-model.md); the queries each tool runs, with the algorithm choice
justified, in [docs/queries.md](docs/queries.md).

```
  Person ─OWNS→ Service ─DEPENDS_ON→ Service ←FIRED_ON─ Alert
     ↑               ↑                   │                 │
 AUTHORED_BY     TOUCHED             EXHIBITS          TRIGGERED_BY
     │               │                   ↓                 ↓
  Commit ──IN──→ Deploy             FailureMode ←FIXES─ Runbook
     ↑                                                     │
     └──────────── CAUSED_BY ──── Incident ────IMPACTED────┘
                                      │
                                   SOLVED_BY
                                      ↓
                   Plan ─STEP→ Step ─USED_TOOL→ Tool
                     │           └──OUTCOME→ Result
                     ├─SIMILAR_TO→ Plan
                     └─SUPERSEDED_BY→ Plan
```

`Plan.signature` is a structural fingerprint of the alert — metric kind, service tier, fan-in and
fan-out buckets, whether a deploy landed upstream in the window, severity — computed in Python,
deterministically. Two incidents on completely different services collide when the *situation*
has the same shape, which is the retrieval. Similarity over alert text cannot do that: the alert
strings are unrelated; the topology is what matches.

## Graph algorithms used

| Algorithm | Where | Why not plain Cypher |
|---|---|---|
| `algo.BFS` | blast radius | Depth-bounded reachability over one edge type is exactly the primitive. |
| `algo.SPpaths` | culprit → most important broken service | Returns the path object with a weight; weighted by `calls_per_min`, so the rendered path carries real traffic. |
| `algo.betweenness` | chokepoint score, computed once at seed time | Breaks ties in `upstream_candidates` with a structural fact. |
| `algo.WCC` | fixture sanity check | An orphan service would make a demo query return empty for reasons that look like an agent bug. |

## Data

Entirely synthetic, generated from a fixed RNG seed (rule 6): 40 services, 100 dependency edges,
241 commits, 70 deploys, 90 alerts, 68 resolved incidents, 46 plans. Re-running the seeder
produces an identical graph, which matters because the cold-vs-warm cost comparison is only
honest if both runs see the same world. Spec: [docs/fixtures.md](docs/fixtures.md).

Three scenario classes, each injectable by name:

- **A — repeated shape.** Eight resolved incidents sharing one signature across several
  services, plus two alerts held back for the live demo: the first runs cold and writes a plan,
  the second recalls it.
- **B — novel shape.** A tier-3 queue-depth alert with no deploy in its window, where the honest
  answer is "no culprit commit, the cause is structural".
- **C — the plan that was wrong.** A plan with `runs: 4, successes: 2`, superseded, with a
  `Tool-[:FAILS_ON]->Condition` edge recording why. Storing failure is what makes this a memory
  rather than a cache.

## What FalkorDB actually does, as opposed to what the docs say

Found by running the queries; each is pinned in `tests/test_falkordb_semantics.py` so an image
upgrade breaks a test instead of the demo. Details in [docs/decisions.md](docs/decisions.md).

- `algo.BFS` at **depth 0 reaches nothing**, though the docs say 0 means unlimited — it returns
  an empty blast radius with no error.
- `algo.BFS` **excludes its source node**, and takes positional arguments only (no config map,
  unlike `algo.WCC` and `algo.SPpaths`).
- A named variable-length relationship binds to a **Path, not a list**: `ALL(d IN deps …)` and
  `size(deps)` raise; use `relationships(deps)` and `length(deps)`.
- Variable-length **bounds cannot be parameterised** — `*1..$hops` raises.
- Vector `score` is a **distance**: nearest neighbours are `ORDER BY score ASC`. The docs' own
  example orders `DESC`, which returns the least similar rows first.

## Documents

| | |
|---|---|
| [SUBMISSION.md](SUBMISSION.md) | the submission write-up, including what went wrong |
| [docs/measurements/](docs/measurements/) | the raw ledger behind every number here |
| [docs/video.md](docs/video.md) | demo video shot list |
| [docs/graph-model.md](docs/graph-model.md) | nodes, edges, properties, indexes |
| [docs/queries.md](docs/queries.md) | the query behind each agent tool |
| [docs/architecture.md](docs/architecture.md) | agent loop, confidence formula, cost ledger |
| [docs/fixtures.md](docs/fixtures.md) | synthetic data spec |
| [docs/decisions.md](docs/decisions.md) | FalkorDB capabilities, verified |
| [docs/walkthrough.md](docs/walkthrough.md) | explain-it-to-a-judge notes |
