# The queries the product depends on

Sketches, not tested code — they get rewritten against a live FalkorDB on Day 1. One section per
agent tool. Every tool is a thin wrapper: the Cypher does the reasoning, the tool returns rows.

Four FalkorDB-specific idioms used throughout. The first two are documented limitations; the
last two were found by running the queries, and both raise rather than mislead:

1. **Relationship uniqueness.** When a relationship in a pattern is not referenced elsewhere in
   the query, FalkorDB only verifies that *at least one* matching relationship exists, so counts
   over such patterns are wrong. Every aggregation below therefore references its edge alias
   (`WHERE ID(e) >= 0`).
2. **No `<>` on indexed properties.** Indexes do not optimize not-equal filters, so exclusions
   are expressed as positive predicates or handled in Python.
3. **A named variable-length relationship is a Path, not a list.** `[deps:DEPENDS_ON*1..3]`
   binds to a Path, so the guard above becomes
   `ALL(d IN relationships(deps) WHERE ID(d) >= 0)` and the hop count is `length(deps)`.
   `size(deps)` raises `Type mismatch: expected List but was Path`.
4. **Variable-length bounds cannot be parameterised.** `*1..$hops` raises
   `Encountered unhandled type in inlined properties`, so a depth argument is interpolated into
   the query string as a validated integer — never bound as a parameter.
5. **`exists()` rejects traversal patterns.** `WHERE NOT EXISTS((p)-[:SUPERSEDED_BY]->(:Plan))`
   raises `Invalid input: traversal patterns are not allowed as arguments to exists()`. The
   working form is `OPTIONAL MATCH` followed by `WITH … WHERE <alias> IS NULL`.

---

## `upstream_candidates(service, hops)`

Reverse the dependency edges from the failing service and rank what could have caused it.
Ranking is structural: closer beats farther, critical edges beat best-effort, tier-1 beats tier-3.

```cypher
MATCH (failing:Service {name: $service})
MATCH path = (cand:Service)-[deps:DEPENDS_ON*1..3]->(failing)   -- depth interpolated, not bound
WHERE ALL(d IN relationships(deps) WHERE ID(d) >= 0)
WITH cand,
     min(length(path))                                       AS distance,
     max([d IN deps | CASE WHEN d.critical THEN 1 ELSE 0 END]) AS on_critical_path
RETURN cand.name AS service, cand.tier AS tier, distance, on_critical_path
ORDER BY on_critical_path DESC, distance ASC, tier ASC
LIMIT 15
```

Note the direction: `(cand)-[:DEPENDS_ON*]->(failing)` reads as "cand depends on failing", so
these are the services whose *callers* include the failing one. The seed data fixes the
convention `(caller)-[:DEPENDS_ON]->(callee)`; the traversal above finds callees-of-callers, i.e.
the things the failing service itself depends on. `fixtures.md` pins the direction and Day 1
verifies it by hand before any tool exists — getting this backwards is the single most likely
silent bug in the project.

## `blast_radius(service)`

Everything downstream of a service, with depth. Uses `algo.bfs` rather than a variable-length
match so the depth comes from the traversal rather than from a `length(path)` aggregation.

```cypher
MATCH (s:Service {name: $service})
CALL algo.BFS(s, 10, 'DEPENDS_ON')
YIELD nodes, edges
RETURN [n IN nodes | n.name] AS impacted, size(nodes) AS count
```

**Three corrections from testing this against a live FalkorDB 6.0.2 (see `decisions.md`):**

1. Procedure names are case-insensitive, so `algo.BFS` and `algo.bfs` both work. We use
   `algo.BFS`, the spelling `dbms.procedures()` reports.
2. **`max_depth: 0` returns nothing, not unlimited** — contrary to the documentation. Depth must
   be an explicit positive integer. Our mesh is 5 layers deep, so 10 is effectively unlimited.
   Left at 0 this query returns an empty blast radius with no error: exactly the
   wrong-but-plausible failure this project is supposed to avoid.
3. **The source node is excluded** from `nodes`, so the count is `size(nodes)`, not
   `size(nodes) - 1`.

`algo.BFS` takes positional arguments only; the config-map form used by `algo.WCC` and
`algo.SPpaths` returns `Received 1 arguments to procedure 'algo.BFS', expected at least 3`.

Per-node depth is not returned, so where the demo needs depth labels, use the explicit form:

```cypher
MATCH (s:Service {name: $service})
MATCH path = (caller:Service)-[deps:DEPENDS_ON*1..5]->(s)
WHERE ALL(d IN relationships(deps) WHERE ID(d) >= 0)
RETURN caller.name AS service, caller.tier AS tier, min(length(path)) AS depth
ORDER BY depth ASC, tier ASC
```

Day 1 decides which of the two the tool ships with, by timing both on the seeded graph. Prefer
`algo.BFS` if it is not slower — a submission that uses FalkorDB's algorithm layer reads better
against Track 01's "graph queries and algorithms as agent tools" criterion than one that only
pattern-matches.

## `deploys_in_window(services, since)`

```cypher
UNWIND $services AS name
MATCH (d:Deploy)-[t:TOUCHED]->(s:Service {name: name})
WHERE d.at >= $since AND ID(t) >= 0
RETURN d.id AS deploy, d.sha AS sha, d.at AS at, s.name AS service
ORDER BY d.at DESC
```

`Deploy.at` is indexed. This is the query that turns "something is wrong" into a bounded set of
changes, and it is why the agent never has to reason about time itself.

## `suspect_commits(deploy_ids, failing_service)`

The ranking that would otherwise be a hallucination risk. A commit is suspect in proportion to
how much its changed files overlap the failing service's owned paths.

```cypher
MATCH (failing:Service {name: $failing_service})
UNWIND $deploy_ids AS did
MATCH (c:Commit)-[inr:IN]->(d:Deploy {id: did})
WHERE ID(inr) >= 0
MATCH (c)-[auth:AUTHORED_BY]->(p:Person)
WHERE ID(auth) >= 0
WITH c, d, p, failing,
     size([f IN c.files WHERE ANY(owned IN failing.paths WHERE f STARTS WITH owned)]) AS overlap
RETURN c.sha AS sha, c.msg AS msg, p.handle AS author,
       d.id AS deploy, overlap, size(c.files) AS files_changed
ORDER BY overlap DESC, d.at DESC
LIMIT 10
```

`overlap` is an integer the agent reports; it is not a model judgement. A tie is resolved by
recency, which is also computed. The agent's `confidence` on `CAUSED_BY` is derived from
`overlap / files_changed` and distance, with the formula in `architecture.md` — written down so
the number means the same thing on every run.

## `owner_of(service)`

```cypher
MATCH (p:Person)-[o:OWNS]->(s:Service {name: $service})
WHERE ID(o) >= 0
RETURN p.handle AS handle, p.name AS name, p.tz AS tz, o.rotation AS rotation
ORDER BY CASE o.rotation WHEN 'primary' THEN 0 ELSE 1 END
```

## `runbook_for(service)`

```cypher
MATCH (s:Service {name: $service})-[e:EXHIBITS]->(fm:FailureMode)<-[f:FIXES]-(r:Runbook)
WHERE ID(e) >= 0 AND ID(f) >= 0
RETURN r.title AS title, r.url AS url, fm.name AS failure_mode, e.seen_count AS seen
ORDER BY e.seen_count DESC
LIMIT 3
```

## `prior_incidents(signature)`

```cypher
MATCH (i:Incident {signature: $signature})-[cb:CAUSED_BY]->(c:Commit)
WHERE ID(cb) >= 0
MATCH (i)-[tb:TRIGGERED_BY]->(a:Alert)-[fo:FIRED_ON]->(s:Service)
WHERE ID(tb) >= 0 AND ID(fo) >= 0
RETURN i.id AS incident, i.at AS at, s.name AS service,
       c.sha AS culprit, cb.confidence AS confidence, cb.verified AS verified
ORDER BY i.at DESC
LIMIT 5
```

## `recall_plan(signature, embedding)`

Two-stage recall. Exact signature match first — cheap, indexed, explainable:

```cypher
MATCH (p:Plan {signature: $signature})
OPTIONAL MATCH (p)-[sb:SUPERSEDED_BY]->(:Plan)
WITH p, sb WHERE sb IS NULL          -- exists() rejects traversal patterns; see note 5
MATCH (p)-[st:STEP]->(s:Step)-[ut:USED_TOOL]->(t:Tool)
WHERE ID(st) >= 0 AND ID(ut) >= 0
OPTIONAL MATCH (s)-[oc:OUTCOME]->(r:Result)
RETURN p.id AS plan, p.runs AS runs, p.successes AS successes,
       p.mean_cost_usd AS mean_cost,
       collect({order: st.order, intent: s.intent, tool: t.name, ok: r.ok}) AS steps
ORDER BY (toFloat(p.successes) / p.runs) DESC
LIMIT 1
```

If that returns nothing, fall back to vector recall over plan embeddings:

```cypher
CALL db.idx.vector.queryNodes('Plan', 'embedding', 5, vecf32($embedding))
YIELD node AS p, score
MATCH (p)-[st:STEP]->(s:Step)-[ut:USED_TOOL]->(t:Tool)
WHERE ID(st) >= 0 AND ID(ut) >= 0
RETURN p.id AS plan, score, p.runs AS runs,
       collect({order: st.order, intent: s.intent, tool: t.name}) AS steps
ORDER BY score DESC
```

The docs warn that vector queries do not combine well with property filters, so the
`SUPERSEDED_BY` check may have to move to Python after the `YIELD`. Day 3 decides; it is a
two-line difference either way.

**Correction from live testing:** `score` is a *distance*, not a similarity — an exact match
scores ~1e-10 and a near match 0.006. So nearest neighbours are `ORDER BY score ASC`. The
documentation's own example orders `DESC`, which returns the *least* similar plans first. Ranking
recall backwards would have been invisible in the demo and fatal to the memory story.

## `record_outcome(...)` — the write-back

One transaction, idempotent on `Incident.id`. This is the edge that makes the next run cheap.

```cypher
MERGE (i:Incident {id: $incident_id})
  ON CREATE SET i.at = $at, i.status = 'resolved', i.signature = $signature
WITH i
MATCH (a:Alert {id: $alert_id})
MERGE (i)-[:TRIGGERED_BY]->(a)
WITH i
MATCH (c:Commit {sha: $culprit_sha})
MERGE (i)-[cb:CAUSED_BY]->(c)
  SET cb.confidence = $confidence, cb.verified = false
WITH i
UNWIND $impacted AS imp
MATCH (s:Service {name: imp.name})
MERGE (i)-[im:IMPACTED]->(s) SET im.depth = imp.depth
WITH DISTINCT i
MERGE (p:Plan {signature: $signature})
  ON CREATE SET p.id = $plan_id, p.runs = 0, p.successes = 0,
                p.embedding = vecf32($embedding)
SET p.runs = p.runs + 1,
    p.successes = p.successes + CASE WHEN $ok THEN 1 ELSE 0 END,
    p.mean_tokens = ((coalesce(p.mean_tokens, 0) * (p.runs - 1)) + $tokens) / p.runs,
    p.mean_cost_usd = ((coalesce(p.mean_cost_usd, 0) * (p.runs - 1)) + $cost) / p.runs
MERGE (i)-[:SOLVED_BY]->(p)
WITH p
UNWIND $steps AS step
MERGE (t:Tool {name: step.tool}) ON CREATE SET t.kind = step.kind
CREATE (s:Step {id: step.id, order: step.order, intent: step.intent,
                tool_name: step.tool, args_shape: step.args_shape})
CREATE (p)-[:STEP {order: step.order}]->(s)
CREATE (s)-[:USED_TOOL]->(t)
CREATE (r:Result {id: step.result_id, ok: step.ok, tokens: step.tokens,
                  ms: step.ms, rows: step.rows})
CREATE (s)-[:OUTCOME]->(r)
```

Caveat to check Day 1: the documented `LIMIT`-does-not-constrain-eager-operations behaviour means
`CREATE`/`MERGE`/`SET` run fully regardless of a trailing `LIMIT`. Nothing above relies on
`LIMIT` to bound a write, but the write-back is the one place where getting that wrong would
corrupt the memory graph, so it gets a round-trip test (`record_outcome` twice on the same
incident must leave `runs = 2` and exactly one `Incident`).

### Superseding a wrong plan

When a replayed plan reaches a culprit a human later rejects:

```cypher
MATCH (old:Plan {id: $old_plan_id})
MERGE (new:Plan {signature: $signature})
  ON CREATE SET new.id = $new_plan_id, new.runs = 0, new.successes = 0
MERGE (old)-[:SUPERSEDED_BY {reason: $reason}]->(new)
WITH old
MATCH (old)-[:STEP]->(s:Step)-[:USED_TOOL]->(t:Tool)
MERGE (cond:Condition {name: $condition})
  ON CREATE SET cond.description = $condition_desc
MERGE (t)-[fo:FAILS_ON]->(cond)
  ON CREATE SET fo.observed_count = 0
SET fo.observed_count = fo.observed_count + 1
```

Recording failure, not just success, is what makes this a memory rather than a cache. It is also
the third scenario class in `fixtures.md` — the demo shows a plan being retired.

## Algorithms used, and why

| Algorithm | Where | Why not plain Cypher |
|---|---|---|
| `algo.BFS` | `blast_radius` | Depth-bounded reachability over one edge type is exactly what it does; faster and clearer than a 5-hop variable-length match. |
| `algo.SPpaths` | explaining *how* a failure reached a tier-1 service | Returns the actual path object with a weight, which is what the demo renders. Weighted by `calls_per_min`, so the path shown is the one carrying real traffic. |
| `algo.betweenness` | offline, once, during seeding | Pre-computes which services are structural chokepoints; stored on `Service.betweenness` and used to break ties in `upstream_candidates`. Computed, not guessed. |
| `algo.WCC` | sanity check on the seeded mesh | A disconnected component means the fixture generator produced an unreachable service, which would make a demo query silently return nothing. |
