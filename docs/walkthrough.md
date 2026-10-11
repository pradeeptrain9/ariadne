# Walkthrough — explain-it-to-a-judge

Rules 12–13: a participant must understand the submitted code and be able to explain the graph
model, the queries, the architecture and the technical decisions. This document is the script for
that conversation. It answers the questions a judge actually asks, in the order they ask them.

Kept current as the code lands — if an answer here stops matching the code, the code is right and
this file is stale, so fix it the same day.

---

## "What does it do?"

An alert fires. The agent answers three questions: **what changed, what else breaks, who owns
it** — and shows the path through the graph that got it there.

Then it remembers. The resolved incident is written back as a reusable *plan*. The next alert
with the same structural shape replays that plan instead of re-investigating, which costs about
an order of magnitude less.

## "Why does this need a graph? Couldn't you do it in Postgres?"

The answer is a path, not a row.

Three of the operations are transitive closures over an edge type: find what the failing service
depends on, transitively; find everything that depends on it, transitively; find the weighted
route from the culprit to the most important service it broke. In SQL those are recursive CTEs
that get rewritten every time the question changes shape, and the output is a set of rows that
has lost the connection structure — you then have to reassemble the path in application code to
explain anything.

Second, and more important for this project: **plan retrieval is a structural lookup.** We find
past work by the *shape* of the situation, then walk `Plan → Step → Tool → Result` to replay it.
That is a subgraph fetch. A relational version is four joins returning a flat result the agent
has to re-nest.

Take FalkorDB out and there is no root-cause path and no replayable memory. The product is
nothing — which is what rule 3 asks.

## "Walk me through the graph model."

Two subgraphs meeting at `Incident`.

**Infrastructure** — what the agent reasons over. `Service` nodes joined by `DEPENDS_ON`
(direction: `(caller)-[:DEPENDS_ON]->(callee)`, so a tier-1 API points at the tier-3 database it
calls). `Deploy` nodes `TOUCHED` services; `Commit` nodes are `IN` deploys and `AUTHORED_BY`
people; people `OWN` services with a rotation. `Alert` nodes `FIRED_ON` a service. `Service`
nodes `EXHIBITS` known `FailureMode`s, and `Runbook`s `FIXES` those.

**Procedural memory** — what the agent writes. `Plan` nodes keyed on a structural signature, each
with ordered `Step`s that `USED_TOOL` and produced a `Result`. `SUPERSEDED_BY` retires a plan
that turned out wrong; `Tool-[:FAILS_ON]->Condition` records what misled it.

`Incident` is the hinge: `TRIGGERED_BY` an alert, `CAUSED_BY` a commit, `IMPACTED` services,
`SOLVED_BY` a plan.

Full property tables: `graph-model.md`.

## "What is the signature, exactly?"

A twelve-character hash of six **structural** facts about the alert — metric kind, service tier,
fan-in bucket, fan-out bucket, whether a deploy landed upstream in the window, severity. Computed
in Python, deterministically, before the model is called.

The point: two incidents on *completely different services* collide on signature when the
situation has the same shape. A tier-1 leaf with high fan-in, a latency alert, a recent upstream
deploy — that is the same problem whether it happens to the checkout API or the search API, and
the stored plan transfers.

**Why not embed the alert text and use vector search?** Because the alert texts are not similar.
"p99 latency on checkout-api" and "p99 latency on search-api" are similar strings but that is
coincidence; what makes the plan reusable is the topology around the service, which the text does
not mention at all. The vector index is in the design, but as the *secondary* net for near-misses
— signatures that differ by one bucket. Exact structural match comes first because it is cheap,
indexed, and explainable.

**Why not ask the model to produce the signature?** It would drift between runs. Plan recall and
prompt caching both depend on byte-stability, so a model-generated key would quietly destroy the
cost numbers that are the whole Track 02 claim.

## "How does the agent decide which commit is guilty? Is that the model guessing?"

No. It is arithmetic over graph results.

`suspect_commits` computes, per commit in the deploy window, how many of its changed files fall
under the failing service's owned path prefixes — `overlap`. Commits are ordered by `overlap`,
ties broken by recency. Then:

```
confidence = clamp(0.1, 0.95,
      0.55 * (overlap / max(1, files_changed))
    + 0.25 * (1 / distance)
    + 0.20 * (1 if deploy_within_30min else 0.4))
```

Every term comes from a Cypher result: `overlap` and `files_changed` from the commit, `distance`
from the dependency traversal, the deploy time from the `Deploy` node. Asked where a 0.78 came
from, the answer is the three numbers that produced it.

The model's job is choosing which tool to call next and writing the explanation. Remove the model
and the verdict is unchanged — it just reads worse. That division is deliberate: a model asked
*which commit broke this* answers fluently and sometimes wrongly, and nothing downstream can
detect it, because a right answer and a wrong answer are both well-formed prose.

## "Show me the queries."

`queries.md`, one section per tool, with the algorithm choice justified per query. The four that
matter:

- `upstream_candidates` — reverse variable-length `DEPENDS_ON` traversal, ranked by whether the
  path was marked critical, then distance, then tier.
- `blast_radius` — variable-length `DEPENDS_ON` match over incoming edges, with depth.
- `suspect_commits` — the file-overlap computation above.
- `recall_plan` — signature lookup on an indexed property, falling back to
  `db.idx.vector.queryNodes` for near-misses.

## "Which FalkorDB algorithms do you use, and why those?"

| Algorithm | Where it runs | Why |
|---|---|---|
| `algo.betweenness` | `seed.py`, once at seed time | Chokepoint score per service, stored on `Service.betweenness` and used as the last ordering key in `upstream_candidates`. Computed, not guessed. |
| `algo.WCC` | `verify.py` | Asserts the seeded mesh is one component — an orphan service would make a demo query return empty for reasons that look like an agent bug. |
| `algo.BFS` | `verify.py` | Cross-checks the variable-length traversal and pins FalkorDB's documented-but-false depth-0 behaviour. **Not** used for blast radius: BFS follows edges in their stored direction, which with `(caller)-[:DEPENDS_ON]->(callee)` walks away from the callers, and it returns no per-node depth. |
| `db.idx.vector.queryNodes` | `verify.py` | Proves the `Plan.embedding` index works and that `score` is a distance, not a similarity. |

Blast radius and the dependency cone are variable-length pattern matches rather than algorithm
calls, for the direction and depth reasons above. `docs/queries.md` has every query verbatim, and
a test asserts the document matches the code.

If asked why the agent's own traversals are not algorithm calls: we tried. `algo.BFS` was the
original blast-radius implementation and it answers the opposite question, because it follows
`DEPENDS_ON` in the direction it is stored. The variable-length match is not a fallback from
failing to use the algorithm layer; it is the correct primitive for "who calls this,
transitively, and how far away are they".

Two things earlier design notes claimed that the code does not do, listed in
`docs/queries.md` under "Designed, not implemented": vector recall as a near-miss fallback in
`recall_plan` (the index exists and is verified, but recall matches the exact signature only),
and `algo.SPpaths` for a weighted culprit→victim path (never called anywhere). Both were in the
notes before the code existed and neither earned its way in.

## "What did FalkorDB make you work around?"

Six, and only two of them are in the documentation as limitations. The rest came from running
the queries — all of them are pinned as regression tests in `tests/test_falkordb_semantics.py`:

1. **Relationship uniqueness.** If a relationship in a pattern is not referenced elsewhere in the
   query, FalkorDB only checks that at least one matching relationship exists — so counts over
   such patterns are wrong. Every aggregation references its edge alias (`WHERE ID(e) >= 0`).
   This was the likeliest source of a wrong-but-plausible number in the whole project.
2. **`LIMIT` does not constrain eager operations.** Writes run fully before `LIMIT` applies, so
   nothing in the write-back relies on `LIMIT` to bound what it writes. `record_outcome` has an
   idempotency test instead: called twice on one incident, `runs` must be 2 and there must be
   exactly one `Incident`.
3. **Indexes do not optimize `<>`.** No query filters not-equal over an indexed property.
4. **Vector queries do not combine with property filters.** The "exclude superseded plans" check
   happens in Python after the `YIELD`, not as a `WHERE` on the procedure output.
5. **A named variable-length relationship binds to a Path, not a list.** `[deps:DEPENDS_ON*1..3]`
   makes `ALL(d IN deps …)` and `size(deps)` raise `Type mismatch: expected List but was Path`.
   The idiom is `relationships(deps)` and `length(deps)`. This one at least fails loudly.
6. **`algo.BFS` at depth 0 reaches nothing**, though the documentation says 0 means unlimited.
   This is the dangerous one: it returns an empty blast radius with no error. It is why the Day 1
   exit criterion is hand-verified traversals before any agent code exists, and why there is a
   test asserting the depth-0 behaviour rather than a comment hoping someone remembers.

Also worth saying because it would otherwise look like sloppiness in the notes: we first
concluded that `algo.bfs` in lowercase was broken. It is not — procedure names are
case-insensitive, and the empty result had come from the depth-0 bug in the same query. Two
variables changed at once. The corrected finding is in `docs/decisions.md`.

## "Did the memory actually save anything? Show me."

Measured on the same alerts in both arms: cold 8 tool calls / 36,017 tokens / $0.1281, warm 4
calls / 25,261 tokens / $0.0681. 1.9× cheaper, 2× fewer calls, same culprit commit every time.

**It did not work the first time, and the failure is the interesting part.** The first design
stored each plan as a transcript of the investigation that produced it. Replaying that transcript
cost the same as deriving it — measured at 1.1×, with warm runs burning *more* tokens than cold
because the plan steps were injected into the prompt and the agent still did all the work. A
memory that replays everything saves nothing.

What fixed it: `recall_plan` now returns a **hypothesis** computed from the graph — "in 13 of 13
prior incidents of this shape, the cause was a commit in a deploy that touched the failing
service itself" — bundled with the *current* blast radius, owner and runbook, all recomputed in
the same call so none of it is stale. A hit then leaves exactly one thing to establish: whether
that kind of cause is present in this window. Two tool calls to verify, one to record.

The honest framing: the saving is not "replay is faster than thinking". It is that the memory
turns an open-ended search into a single hypothesis test, and bundles four lookups into one.

## "Why is the second answer cheaper? Convince me it isn't just caching."

Three separable effects, and the chart attributes them separately rather than claiming all of it
for the memory:

1. **Fewer tool calls.** A cold investigation runs 12–15; a replay runs 3–5. Fewer round trips,
   fewer tool results in context. This is the memory.
2. **Lower effort.** `output_config.effort` is `high` cold, `low` on replay — a replay follows a
   known sequence rather than reasoning from scratch, so low effort is *correct* there, not just
   cheaper.
3. **Prompt caching.** Tool definitions, system prompt and the frozen schema block are the stable
   prefix; the alert payload goes after the last breakpoint. Verified, not assumed — a test
   asserts `cache_read_input_tokens > 0` on the second call of a session, because a stray
   timestamp in the system prompt would silently invalidate the prefix and the headline number
   would quietly be wrong.

And the accuracy guard: `replay.py` asserts the warm path reaches the *same culprit commit* as
the cold path. A cheap wrong answer is worse than an expensive right one.

## "What happens when there's no deploy to blame?"

Scenario B in the fixtures exists for this. A tier-3 service, a `queue_depth` alert, nothing
deployed in the window — the cause is an upstream saturation cascade, not a code change. The
correct answer is "no culprit commit; the cause is structural", plus the path to the saturated
dependency.

An agent that cannot say *not a deploy* will confidently blame the nearest commit. The fixture is
built so that the honest answer is the checkable one.

## "Does the memory ever learn the wrong thing?"

Yes, and the graph records it. Scenario C is a plan with `runs: 4, successes: 2`, already
`SUPERSEDED_BY` a corrected plan, with a `FAILS_ON` edge naming the condition that broke it: file
overlap ranks badly when the culprit commit touches a shared library and barely touches the
failing service's own paths.

Storing failures is what makes this a memory rather than a cache. It is also one query to show.

## "What's synthetic here, and is that a problem?"

All of it, by choice and by rule 6 — generated by our own seed script from a fixed RNG seed, so
the graph is byte-identical on every run. That matters for the cost comparison: cold and warm
must see the same world or the ratio means nothing.

We deliberately did not build a Datadog or GitHub ingestion pipeline. The graph model and the
agent are what gets judged; an ETL layer would have consumed a day and proved nothing about
either. The schema is shaped to accept real data — the seed script is the only thing that would
be replaced.

## "What would you do next?"

1. Real ingestion behind the same schema — GitHub deploys and commits first, since that is the
   half with a clean API.
2. Human verification loop on `CAUSED_BY.verified`, so plan reinforcement learns from confirmed
   outcomes rather than the agent's own confidence.
3. Multi-tenant memory: a `tenant` property and per-tenant plan recall, which the signature
   design already permits.
4. Cross-service plan transfer measurement — how often a plan learned on one service correctly
   transfers to another. The data to measure it is already in the graph.

## Honest limitations

- 40 services is a small mesh. Traversal costs are not the interesting problem at this size, and
  we do not claim they are.
- Plan replay is verified against the culprit commit, not against a full re-investigation, so a
  plan that is right for the wrong reason would survive.
- The embedding path is the weakest part of the design: it is a near-miss net over a few hundred
  plans, and at this scale a Python cosine loop would work as well as the HNSW index. It is in
  because the structure scales, not because it is load-bearing today.
- `Tool-[:FAILS_ON]->Condition` is written by the agent, so the taxonomy of failure conditions is
  as good as the agent's own description of what went wrong.
