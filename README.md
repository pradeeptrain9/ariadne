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
| Agent loop (tool runner, caching, effort policy) | run — six investigations in `runs.jsonl` |
| Cold-vs-warm measurement harness | run at **3 alerts per arm**; the ledger holds those six runs |
| Widening that to 20 alerts per arm | **not done** — needs an API key; pre-flighted and ready |
| MCP server (stdio, read-only by default) | done |
| Demo page with live streaming run | path covered by tests; the model call in it is not |
| Submission write-up, measurements, video shot list | done |
| Demo video | not recorded |

Everything below is measured, not projected — 127 tests, and the numbers come out of
`runs.jsonl`. The sample is small: three alerts per arm, stated wherever the ratio is.

**What a missing API key leaves unverified.** The tests need no key, and they now drive the
streaming path end to end — real socket, real handler, real tool bodies against the real graph,
real write-back — with a scripted model client in place of the one call that needs a credential
(`tests/test_sse_stream.py`). So the framing, the event order, the per-call row counts and the
ledger row are all checked. What is *not* checked is whether the model, given this system prompt
and these tools, picks that sequence: that is a live run, and it is the same credential the
20-alert measurement is waiting on. `python -m ariadne.replay --n 20 --dry-run` reports exactly
what that run would do, and costs nothing.

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

The rule is easy to state and easy to break. Until Day 2 the verdict's **confidence** was the
model's own number: `record_outcome` took it as an argument and wrote it to the graph, and the
seeded history filled 68 more from `rng.uniform(0.62, 0.93)` — which `recall_plan` then handed
back to the model as evidence. The one figure in the verdict that reads like a measurement was
the only one nothing computed. It is now `scoring.confidence` over graph facts, recomputed at
write time, stored with its terms on the `CAUSED_BY` edge, and asserted by a test; the model's
estimate is kept beside it as `model_confidence` and used for nothing. Both halves of the fix
are in [docs/architecture.md](docs/architecture.md).

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
.venv/bin/python -m pytest -q             # 127 tests, no API key needed
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

Measured on `claude-sonnet-5`, same alerts in both arms, **three alerts per arm** (six runs):

| | runs | tool calls | tokens | cost | wall |
|---|---|---|---|---|---|
| cold (memory withheld) | 3 | 7.0 | 36,320 | $0.0503 | 28.0s |
| warm (memory available) | 3 | 4.0 | 25,867 | $0.0273 | 16.2s |

**1.8× cheaper, 1.8× fewer tool calls, same culprit commit on every alert.** Warm runs also
report higher confidence, because the recalled hypothesis corroborates what the current window
shows. Three alerts per arm is a small sample — it is the ledger's whole contents, and
`python -m ariadne.replay --n 20` widens it. That run has not been made: it needs an API key.
What it would do is checkable without one: `--dry-run` reports 20 alerts available of
68 in the graph, 40 runs to make, and **$1.55** estimated — 20 x (mean cold + mean warm) from
the rows already in `docs/measurements/runs.jsonl`, not a guess — then names the missing
credential and exits non-zero. The harness refuses to start rather than half-measure: fewer alerts in the graph than
asked for, a model with no price recorded, or a missing credential each stop it before the first
call. `--resume` skips the arms a ledger already holds, so 40 runs do not have to survive in one
sitting.

The comparison is the same alerts against the same graph, not a warm run on one alert versus a
cold run on another — that would measure the alerts. `replay.py` asserts the warm arm reached the
same culprit as the cold arm and exits non-zero otherwise; a cheap wrong answer is worse than an
expensive right one.

Honest caveat: **the first design of this measured 1.1×.** The plan stored a transcript of the
whole investigation, so replaying it was the same work — memory that replays everything saves
nothing. What fixed it was making `recall_plan` return a *hypothesis* computed from the graph
("in 9 of 9 prior incidents of this shape, the cause was a commit in a deploy that touched the
failing service itself") bundled with the current impact, owner and runbook. A hit then leaves
exactly one thing to establish, and the warm path is 4 calls instead of 7. The failure and the
fix are both in `docs/walkthrough.md`.

The same comparison on `claude-opus-5` measured 1.9× (cold $0.1281, warm $0.0677) before the
project moved to Sonnet 5 for cost. The ratio survived the model change, which is the point of
measuring a ratio rather than an absolute.

Prompt caching is verified rather than assumed — `--check-cache` writes the prefix on the first
call and reads all of it back on the second.

## Demo

```bash
.venv/bin/python -m ariadne.server        # http://127.0.0.1:8000
```

Needs `ANTHROPIC_API_KEY` — the page drives the real agent.

Pick an alert, press Investigate. Tool calls stream in over SSE as they happen, the cost counter
moves, and the cold-vs-warm bars update from the ledger when the run finishes. "Withhold memory"
forces the cold path on the same alert, which is the A/B in one click.

Two traversals are drawn in two colours, because they answer two different questions: the
**dependency cone** the agent searched for a cause (upstream, blue dashed — the set
`upstream_candidates` actually returned, not a recomputation) and the **blast radius** that
breaks with the failing service (downstream, red).

The verdict panel shows the **computed verdict first**, read back from the graph through
`/api/verdict`: the culprit, the chain from alert → service → deploy → commit → author, the
confidence with the three terms that produced it, the impact, the owner and the runbook. The
model's paragraph sits underneath it, labelled as the explanation rather than the answer. Where
the two disagree, the card is the one that is checkable.

With no local `runs.jsonl`, the bars fall back to the committed measurements and the caption
says which file they came from — a judge opening the page before running anything still sees the
comparison, and never mistakes it for a run on their own machine.

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

| Algorithm | Where it runs | Why |
|---|---|---|
| `algo.betweenness` | `seed.py`, once at seed time | Chokepoint score per service, stored on `Service.betweenness` and used as the last ordering key in `upstream_candidates`. Computed, not guessed. |
| `algo.WCC` | `verify.py` | Asserts the seeded mesh is one component — an orphan service would make a demo query return empty for reasons that look like an agent bug. |
| `algo.BFS` | `verify.py` | Cross-checks the variable-length traversal and pins FalkorDB's documented-but-false depth-0 behaviour. **Not** used for blast radius: BFS follows edges in their stored direction, which with `(caller)-[:DEPENDS_ON]->(callee)` walks away from the callers, and it returns no per-node depth. |
| `db.idx.vector.queryNodes` | `verify.py` | Proves the `Plan.embedding` index works and that `score` is a distance, not a similarity. |

Blast radius and the dependency cone are variable-length pattern matches rather than algorithm
calls, for the direction and depth reasons above. `docs/queries.md` has every query verbatim, and
a test asserts the document matches the code.

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
  unlike `algo.WCC`).
- A named variable-length relationship binds to a **Path, not a list**: `ALL(d IN deps …)` and
  `size(deps)` raise; use `relationships(deps)` and `length(deps)`.
- Variable-length **bounds cannot be parameterised** — `*1..$hops` raises.
- Vector `score` is a **distance**: nearest neighbours are `ORDER BY score ASC`. The docs' own
  example orders `DESC`, which returns the least similar rows first.
- `shortestPath` is legal **only in a `WITH` or `RETURN` clause** — `MATCH p = shortestPath(…)`,
  the spelling every Cypher example uses, raises.
- A node is **not at distance 0 from itself** under `*1..n`: the shortest path to itself comes
  back empty with a `NULL` length, not as a one-node path.

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
