# Ariadne — submission

**Tracks:** Agents That Act on Connected Data · Agent Memory and Coordination
**Graph:** FalkorDB 6.0.2 (Docker, pinned by digest)
**Repo:** public, MIT · **Demo:** `docker compose up -d && python -m ariadne.server`

---

## Inspiration

An alert fires at 2am. The on-call engineer spends the next forty minutes answering three
questions that the company already knows the answers to: *what changed, what else breaks, who
owns it.* The deploy history knows. The dependency graph knows. The last six incidents that
looked exactly like this one knew too, and nobody wrote it down in a form anything can read.

The reason an agent does not fix this by default is that each of those questions is a question
about **relationships**, and an agent handed a pile of logs sees rows. It cannot follow a
dependency three hops upstream, it cannot tell you what else goes down with a service, and when
it finishes it forgets, so the seventh identical incident costs exactly what the first one did.

## What it does

An alert goes in. A verdict comes out: the culprit commit, the confidence and what that number
was computed from, every service affected, the owner to page, the runbook — and the path through
the graph that produced it.

Then it remembers. The resolved incident is written back as a plan keyed on the *structural
shape* of the situation. The next alert with the same shape — on a different service, with
unrelated text — recalls it, and costs half as much.

Two things we decided early and did not bend on.

**Ranking is computed, never asked of a model.** Cypher traversal and FalkorDB's graph algorithms
decide what is upstream, what the blast radius is, and which commit overlaps the failing
service's code. Claude chooses which tool to call and writes the explanation. Remove the model
and the verdict is unchanged; it just reads worse. A model asked *which commit broke this*
answers fluently and sometimes wrongly, and nothing downstream can tell, because a right answer
and a wrong one are both well-formed prose.

**The explanation is the artifact, not a narration of one.** The answer *is* a path, so there is
nothing to reconstruct after the fact.

## The graph

Two subgraphs meeting at `Incident`.

Infrastructure: `Service` nodes joined by `DEPENDS_ON` (caller → callee), `Deploy` nodes that
`TOUCHED` services, `Commit` nodes `IN` deploys and `AUTHORED_BY` people, people who `OWN`
services on a rotation, `Alert` nodes that `FIRED_ON` a service, and `Runbook`s that `FIXES` the
`FailureMode`s a service `EXHIBITS`.

Procedural memory: `Plan` nodes keyed on a structural signature, with ordered `Step`s that
`USED_TOOL` and produced a `Result`; `SUPERSEDED_BY` retires a plan that turned out wrong, and
`Tool-[:FAILS_ON]->Condition` records what misled it.

### The signature is the idea

Twelve characters of SHA-1 over six structural facts: metric kind, service tier, fan-in bucket,
fan-out bucket, whether a deploy landed upstream in the window, severity. Computed in Python,
deterministically, before the model is called.

Two incidents on completely different services collide when the *situation* has the same shape.
A tier-1 service with high fan-in, a latency alert, a recent upstream deploy — that is the same
problem whether it happens to checkout or to search, and the stored plan transfers.

Embedding the alert text cannot do this. "p99 latency on checkout-api" and "p99 latency on
search-api" are similar strings by coincidence; what makes the plan reusable is the topology
around the service, which the text never mentions. The vector index is in the design as the
*secondary* net for near-misses, and we say so rather than overselling it.

## How we built it

Python 3.11, FalkorDB in Docker, the Anthropic SDK's tool runner, `claude-opus-5`. Nine tools,
each a thin wrapper over one Cypher query. One registry, two transports: the same functions are
served over MCP (stdio, read-only by default), and a test asserts the two surfaces cannot drift.

Graph algorithms earn their place rather than decorating the README:

| Algorithm | Use |
|---|---|
| `algo.BFS` | blast radius — depth-bounded reachability over one edge type |
| `algo.SPpaths` | the weighted path from culprit to the most important broken service |
| `algo.betweenness` | chokepoint score per service, computed at seed time, breaks ties structurally |
| `algo.WCC` | fixture sanity check — an orphan service would make a demo query return empty |

Data is entirely synthetic from a fixed RNG seed: 40 services, 100 dependency edges, 241 commits,
70 deploys, 90 alerts, 68 resolved incidents. Re-seeding reproduces it exactly, which matters
because the cost comparison is only honest if both arms see the same world. We deliberately built
no ingestion pipeline — the graph model and the agent are what gets judged, and a Datadog
connector would have cost a day and proved nothing about either.

## What the memory is worth

Same alerts, same graph, memory withheld versus available:

| | tool calls | tokens | cost | wall |
|---|---|---|---|---|
| cold | 8.0 | 36,017 | $0.1281 | 32.9s |
| warm | 4.0 | 25,288 | $0.0677 | 19.3s |

**1.9× cheaper, 2× fewer tool calls, the same culprit commit on every alert.** Warm runs reported
*higher* confidence (0.72–0.80 vs 0.45–0.55), because the recalled hypothesis corroborates what
the current window shows. Raw ledger and per-run detail: `docs/measurements/`.

### It did not work the first time

The first design measured **1.1×**, with warm runs burning *more* tokens than cold.

Plans stored a transcript of the investigation that produced them, so replaying one cost what
deriving it cost — and the injected steps made the prompt bigger. A memory that replays
everything saves nothing. The mechanism worked (same culprit both arms); the economics did not
exist.

What fixed it: `recall_plan` now returns a **hypothesis computed from the graph** — *"in 13 of 13
prior incidents of this shape, the cause was a commit in a deploy that touched the failing
service itself"* — bundled with the current blast radius, owner and runbook, all recomputed in
the same call so none of it is stale. A hit then leaves exactly one thing to establish: whether
that kind of cause is present in this window. Two calls to verify, one to record.

The saving is not "replay beats thinking". It is that memory turns an open-ended search into a
single hypothesis test, and collapses four lookups into one.

## Challenges

**The documentation and the server disagree, silently.** `algo.BFS` at `max_depth: 0` is
documented as unlimited; it reaches nothing and raises no error, so a blast radius comes back
empty and plausible. Vector `score` is a *distance*, while the docs' own example sorts `DESC` —
which ranks recall backwards. A named variable-length relationship binds to a Path, not a list,
so `ALL(d IN deps …)` raises. `exists()` rejects traversal patterns. Variable-length bounds
cannot be parameterised. Each of these is now a regression test in
`tests/test_falkordb_semantics.py`, so an image upgrade breaks a test instead of the demo.

**Getting the direction wrong would have been invisible.** `(caller)-[:DEPENDS_ON]->(callee)`
reversed still returns plausible rows from every traversal in the project. It is pinned by a test
and was hand-verified against a drawn subgraph before any agent code existed.

**Our own fixtures lied to us twice.** Scenario A first selected tier-1 services with no callers,
so the blast radius in the headline demo was empty. And `recall_plan` correctly skipped a
superseded plan, returning its replacement — which had no steps, so a memory "hit" gave the agent
nothing to act on. Worse than a miss.

## What we learned

Procedural memory is not a cache of transcripts. The useful thing to store is the *shape of the
answer*, because that is what turns a search into a test. We only found that out by measuring,
and the measurement was the thing that nearly sank the entry.

Storing failure matters as much as storing success: a plan with `runs: 4, successes: 2`,
superseded, with a `FAILS_ON` edge naming the condition that broke it — file-overlap ranking
scores a shared-library cause near zero. The agent is told about that blind spot and flags such
commits explicitly instead of silently promoting one.

## What's next

Real ingestion behind the same schema, starting with GitHub. Human verification on
`CAUSED_BY.verified` so plans reinforce on confirmed outcomes rather than the agent's own
confidence. Per-tenant memory, which the signature design already permits. And measuring
cross-service plan transfer, for which the data is already in the graph.

## Honest limitations

- 40 services is a small mesh; traversal cost is not the interesting problem at this size and we
  do not claim it is.
- Warm runs are verified against the culprit commit, not against a full re-investigation, so a
  plan that is right for the wrong reason would survive.
- The embedding path is the weakest part of the design — a near-miss net over a few hundred
  plans, where a Python cosine loop would do as well. It is in because the structure scales, not
  because it is load-bearing today.
- `Tool-[:FAILS_ON]->Condition` is written by the agent, so the failure taxonomy is only as good
  as the agent's description of what went wrong.

## Rule compliance

- **Rule 2/3 — FalkorDB central:** remove it and there is no root-cause path and no memory. The
  product is nothing.
- **Rule 5 — tracks:** both named above; one project, as the rules allow.
- **Rule 6 — data:** entirely synthetic, generated by our own seed script. Nothing private.
- **Rule 8 — timing:** stated plainly in the README. Design notes in `docs/` were written
  2026-10-11, before the window opened, as rule 8 permits. Code was also written before the
  window — earlier than rule 8 intends. Recorded rather than disguised; commits carry real dates.
- **Rule 10 — artifacts:** public repo, README with setup and the graph model, the Cypher and
  algorithms the product depends on (`docs/queries.md`), demo video, local setup instructions,
  tracks named.
- **Rule 11 — AI disclosure:** built with Claude Code (`claude-opus-5`) — design notes, the
  seeder, tools, agent loop, tests and this document. Every FalkorDB behaviour in
  `docs/decisions.md` was established by running queries against a live instance, not taken from
  the model's or the documentation's word.
- **Rules 12–13 — understanding:** `docs/walkthrough.md` is the explain-it-to-a-judge document —
  every query in plain language, why the signature is computed rather than generated, and where
  the design failed and what fixed it.
