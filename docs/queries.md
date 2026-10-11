# The queries the product depends on

Every query below is copied verbatim from the source, and
`tests/test_docs_match_code.py` asserts it stays that way — if a query changes in the code and
not here, that test fails. An earlier version of this file was written before the code and drifted
from it badly enough to document a fallback that was never wired in, which is why the check
exists.

Each tool is a thin wrapper: the graph does the reasoning, the tool returns rows. Nothing here
asks the model to rank, score or judge.

## FalkorDB idioms used throughout

Five constraints shape how these are written. The first two are documented limitations; the rest
were found by running the queries, and all five are pinned in `tests/test_falkordb_semantics.py`.

1. **Relationship uniqueness.** When a relationship in a pattern is not referenced elsewhere in
   the query, FalkorDB only verifies that *at least one* matching relationship exists, so counts
   over such patterns are wrong. Every aggregation references its edge alias (`WHERE ID(e) >= 0`).
2. **No `<>` on indexed properties.** Indexes do not optimize not-equal filters, so exclusions
   are positive predicates or happen in Python.
3. **A named variable-length relationship is a Path, not a list.** `ALL(d IN deps …)` and
   `size(deps)` raise `Type mismatch: expected List but was Path`; the working idiom is
   `ALL(d IN relationships(deps) …)` and `length(deps)`.
4. **Variable-length bounds cannot be parameterised.** `*1..$hops` raises, so a depth argument is
   interpolated into the query string — which is why `tools._hops` validates it as an integer in
   range before it ever reaches Cypher.
5. **`exists()` rejects traversal patterns.** `WHERE NOT EXISTS((p)-[:SUPERSEDED_BY]->(:Plan))`
   raises; the working form is `OPTIONAL MATCH` then `WITH … WHERE <alias> IS NULL`.

---

## `signature.shape_of` — the structural facts behind the memory key

Not a tool. Runs before the model is called, and its result is hashed into the signature that
the whole procedural memory is indexed by.

```cypher
MATCH (s:Service {name: $service})
OPTIONAL MATCH (caller:Service)-[cin:DEPENDS_ON]->(s)
  WHERE ID(cin) >= 0
WITH s, count(DISTINCT caller) AS fan_in
OPTIONAL MATCH (s)-[cout:DEPENDS_ON]->(callee:Service)
  WHERE ID(cout) >= 0
WITH s, fan_in, count(DISTINCT callee) AS fan_out
OPTIONAL MATCH (up:Service)<-[deps:DEPENDS_ON*0..2]-(s)
  WHERE ALL(d IN relationships(deps) WHERE ID(d) >= 0)
WITH s, fan_in, fan_out, collect(DISTINCT up.name) AS upstream
OPTIONAL MATCH (d:Deploy)-[t:TOUCHED]->(u:Service)
  WHERE u.name IN upstream AND ID(t) >= 0
    AND d.at <= $at AND d.at >= $at - $window
RETURN s.tier AS tier, fan_in, fan_out, count(d) AS deploys
```

Both degree counts reference their edge alias — without that, FalkorDB's relationship-uniqueness
behaviour returns 1 instead of the true count, every service lands in the same fan-in bucket, and
every signature collides. The memory would look like it was working.

---

## `recall_plan(signature, service)` — memory

Four queries in one tool call. That bundling *is* the saving: a hit answers impact, ownership and
runbook without three more round trips, leaving only the hypothesis to verify.

**The live plan for this shape**, skipping any that has been superseded:

```cypher
MATCH (p:Plan {signature: $sig})
OPTIONAL MATCH (p)-[sb:SUPERSEDED_BY]->(:Plan)
WITH p, sb WHERE sb IS NULL
RETURN p.id AS plan, p.runs AS runs, p.successes AS successes,
       p.mean_cost_usd AS mean_cost_usd
ORDER BY (toFloat(p.successes) / p.runs) DESC LIMIT 1
```

**Where the cause lived, across every prior incident of this shape.** This is the hypothesis the
warm path verifies, and it is computed — not the model recalling something:

```cypher
MATCH (i:Incident {signature: $sig})-[cb:CAUSED_BY]->(c:Commit)
WHERE ID(cb) >= 0
MATCH (i)-[tb:TRIGGERED_BY]->(:Alert)-[fo:FIRED_ON]->(fs:Service)
WHERE ID(tb) >= 0 AND ID(fo) >= 0
MATCH (c)-[inr:IN]->(d:Deploy)-[t:TOUCHED]->(ds:Service)
WHERE ID(inr) >= 0 AND ID(t) >= 0
WITH count(DISTINCT i) AS incidents,
     sum(CASE WHEN ds.name = fs.name THEN 1 ELSE 0 END) AS self_deploys
RETURN incidents, self_deploys
```

**Current impact, owner and runbook**, recomputed now so nothing recalled is stale:

```cypher
MATCH (s:Service {name: $service})
MATCH (caller:Service)-[deps:DEPENDS_ON*1..5]->(s)
WHERE ALL(d IN relationships(deps) WHERE ID(d) >= 0)
RETURN caller.name AS service, caller.tier AS tier, min(length(deps)) AS depth
ORDER BY tier ASC, depth ASC
```

```cypher
MATCH (p:Person)-[o:OWNS]->(s:Service {name: $service})
WHERE ID(o) >= 0
RETURN p.handle AS handle, p.name AS name, p.tz AS tz, o.rotation AS rotation
ORDER BY CASE o.rotation WHEN 'primary' THEN 0 ELSE 1 END
```

```cypher
MATCH (s:Service {name: $service})-[e:EXHIBITS]->(fm:FailureMode)<-[f:FIXES]-(r:Runbook)
WHERE ID(e) >= 0 AND ID(f) >= 0
RETURN r.title AS title, r.url AS url, fm.name AS failure_mode
ORDER BY e.seen_count DESC LIMIT 1
```

## `prior_incidents(signature)`

```cypher
MATCH (i:Incident {signature: $sig})-[cb:CAUSED_BY]->(c:Commit)
WHERE ID(cb) >= 0
MATCH (i)-[tb:TRIGGERED_BY]->(x:Alert)-[fo:FIRED_ON]->(s:Service)
WHERE ID(tb) >= 0 AND ID(fo) >= 0
RETURN i.id AS incident, i.at AS at, s.name AS service, c.sha AS culprit,
       c.msg AS commit_msg, cb.confidence AS confidence, cb.verified AS verified
ORDER BY i.at DESC LIMIT 5
```

`verified: true` means a human confirmed the cause, as opposed to the agent asserting it.

---

## `upstream_candidates(service, hops)`

The dependencies of the failing service, ranked by the graph. `{…}` is the validated `hops`
integer, interpolated because variable-length bounds cannot be bound as parameters.

```cypher
MATCH (failing:Service {name: $service})
MATCH path = (failing)-[deps:DEPENDS_ON*1..{…}]->(cand:Service)
WHERE ALL(d IN relationships(deps) WHERE ID(d) >= 0)
WITH cand, min(length(deps)) AS distance,
     max(CASE WHEN ANY(d IN relationships(deps) WHERE d.critical)
              THEN 1 ELSE 0 END) AS on_critical_path
RETURN cand.name AS service, cand.tier AS tier, distance, on_critical_path,
       cand.betweenness AS betweenness
ORDER BY on_critical_path DESC, distance ASC, tier ASC, betweenness DESC
LIMIT 15
```

Four ordering keys, all computed: whether any edge on the path is marked critical, how many hops
away it is, its tier, and its precomputed betweenness. The last breaks ties with a structural
fact rather than an arbitrary one.

## `deploys_in_window(services, alert_at)`

```cypher
UNWIND $services AS name
MATCH (d:Deploy)-[t:TOUCHED]->(s:Service {name: name})
WHERE ID(t) >= 0 AND d.at <= $at AND d.at >= $at - $window
RETURN DISTINCT d.id AS deploy, d.sha AS sha, d.at AS at, s.name AS service,
       ($at - d.at) / 60000 AS minutes_before_alert
ORDER BY d.at DESC
```

An empty result is an answer: nothing shipped, so no commit can be the cause. The tool attaches
that sentence to the empty case, because an empty array does not say it and the agent will
otherwise nominate a commit anyway.

## `suspect_commits(deploy_ids, failing_service)`

The ranking that would otherwise be the project's main hallucination risk.

```cypher
MATCH (failing:Service {name: $failing})
UNWIND $deploys AS did
MATCH (c:Commit)-[inr:IN]->(d:Deploy {id: did})
WHERE ID(inr) >= 0
MATCH (c)-[auth:AUTHORED_BY]->(p:Person)
WHERE ID(auth) >= 0
WITH c, d, p, failing,
     size([f IN c.files
           WHERE ANY(owned IN failing.paths WHERE f STARTS WITH owned)]) AS overlap
RETURN c.sha AS sha, c.msg AS msg, p.handle AS author, d.id AS deploy,
       d.at AS deploy_at, overlap, size(c.files) AS files_changed, c.files AS files
ORDER BY overlap DESC, d.at DESC LIMIT 10
```

`overlap` is an integer the graph computes. The known blind spot — a shared-library commit scores
near zero while still being the cause — is recorded in the graph as
`(Tool)-[:FAILS_ON]->(Condition)` and stated in the tool description, so the agent flags such
commits instead of silently promoting one.

## `blast_radius(service)`

```cypher
MATCH (s:Service {name: $service})
MATCH (caller:Service)-[deps:DEPENDS_ON*1..5]->(s)
WHERE ALL(d IN relationships(deps) WHERE ID(d) >= 0)
RETURN caller.name AS service, caller.tier AS tier, min(length(deps)) AS depth
ORDER BY tier ASC, depth ASC
```

**Why not `algo.BFS`?** It was the original design and it does not fit. `algo.BFS` follows edges
in their stored direction, and with `(caller)-[:DEPENDS_ON]->(callee)` that walks *away* from the
callers — it answers "what does this service depend on", which is the opposite question. Blast
radius needs incoming edges, and BFS takes no direction argument. The variable-length match also
yields per-node depth, which BFS does not return at all. `algo.BFS` is still used in
`verify.py`, where the direction it does follow is the one being checked.

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
RETURN r.title AS title, r.url AS url, fm.name AS failure_mode,
       fm.symptom AS symptom, e.seen_count AS seen
ORDER BY e.seen_count DESC LIMIT 3
```

---

## `record_outcome(...)` — the write-back

Five statements in one tool call. The *steps* are not an argument: they come from the tool calls
actually made during the run, so a replayed plan reflects what happened rather than what the
model remembers happening.

```cypher
MERGE (i:Incident {id: $iid})
  ON CREATE SET i.at = $at, i.status = $status, i.signature = $sig
WITH i
MATCH (x:Alert {id: $aid})
MERGE (i)-[:TRIGGERED_BY]->(x)
```

```cypher
MATCH (i:Incident {id: $iid}), (c:Commit {sha: $sha})
MERGE (i)-[cb:CAUSED_BY]->(c)
SET cb.confidence = $conf, cb.verified = false
```

Skipped entirely when there is no culprit — a structural cause is a valid verdict, and recording
a commit anyway would poison the memory for every future incident of that shape.

```cypher
MATCH (i:Incident {id: $iid})
UNWIND $names AS name
MATCH (s:Service {name: name})
MERGE (i)-[im:IMPACTED]->(s) ON CREATE SET im.depth = 1
```

```cypher
MERGE (p:Plan {signature: $sig})
  ON CREATE SET p.id = $pid, p.runs = 0, p.successes = 0,
                p.mean_tokens = 0, p.mean_cost_usd = 0.0
SET p.runs = p.runs + 1,
    p.successes = p.successes + CASE WHEN $ok THEN 1 ELSE 0 END,
    p.mean_tokens = ((p.mean_tokens * (p.runs - 1)) + $tokens) / p.runs,
    p.mean_cost_usd = ((p.mean_cost_usd * (p.runs - 1)) + $cost) / p.runs
WITH p
MATCH (i:Incident {id: $iid})
MERGE (i)-[:SOLVED_BY {at: $at}]->(p)
```

`mean_cost_usd` is a rolling mean maintained in Cypher, so "this plan costs $0.07" is a property
a judge can query rather than a claim in a README.

```cypher
UNWIND $rows AS r MERGE (t:Tool {name: r.tool}) ON CREATE SET t.kind = r.kind
```

```cypher
MATCH (p:Plan {signature: $sig})
WITH p LIMIT 1
UNWIND $rows AS r
CREATE (s:Step {id: r.id, order: r.order, intent: r.intent,
                tool_name: r.tool, args_shape: r.args_shape})
CREATE (p)-[:STEP {order: r.order}]->(s)
WITH s, r
MATCH (t:Tool {name: r.tool})
CREATE (s)-[:USED_TOOL]->(t)
CREATE (res:Result {id: r.result_id, ok: r.ok, tokens: r.tokens,
                    ms: r.ms, rows: r.rows})
CREATE (s)-[:OUTCOME]->(res)
```

Everything after `MERGE (p:Plan …)` is an eager write, and FalkorDB runs eager operations fully
regardless of a trailing `LIMIT`. Nothing here relies on `LIMIT` to bound what is written, and
`tests/test_tools.py` asserts the whole write-back is idempotent: called twice on one incident,
`runs` becomes 2 and exactly one `Incident` exists.

---

## Graph algorithms, and where each one actually runs

| Algorithm | Where | Why |
|---|---|---|
| `algo.betweenness` | `seed.py`, once at seed time | Chokepoint score per service, stored on `Service.betweenness` and used as the last ordering key in `upstream_candidates`. Computed, not guessed. |
| `algo.WCC` | `verify.py` | Asserts the seeded mesh is one component. An orphan service would make a demo query return empty for reasons that look like an agent bug. |
| `algo.BFS` | `verify.py` | Cross-checks the variable-length traversal and pins the documented-but-false depth-0 behaviour. Not usable for blast radius — see above. |
| `db.idx.vector.queryNodes` | `verify.py` | Proves the `Plan.embedding` index works and that `score` is a distance. Not yet wired into recall — see below. |

## Designed, not implemented

Listed because the design notes describe them and the code does not do them. Saying so is
cheaper than being asked.

- **Vector recall as a near-miss fallback in `recall_plan`.** The index exists, is populated at
  seed time and is exercised in `verify.py`, but `recall_plan` matches on the exact signature
  only. Plans whose signature differs by one bucket are not found today. At 46 plans the exact
  match hits often enough that the fallback has not earned its complexity.
- **`algo.SPpaths` for a weighted culprit→victim path.** Documented in earlier design notes and
  in `docs/architecture.md` as the thing the demo would draw. It is not called anywhere in the
  product, and the page draws impact from the dependency edges instead. Adding it would be a
  small tool; it has not been added, so no claim depends on it.
