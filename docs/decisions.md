# Decisions and confirmed capabilities

Everything below was read from the FalkorDB documentation during the pre-window, so Day 1 spends
zero time on reference lookup. Each item marked **verify** is confirmed against a running
instance on Day 1 before anything depends on it.

Sources: `docs.falkordb.com/cypher/procedures`, `/algorithms/`, `/algorithms/bfs`,
`/algorithms/sppath`, `/cypher/indexing`, `/cypher/indexing/vector-index`,
`/cypher/known-limitations`. Read 2026-10-11.

## Verified live (2026-10-11, FalkorDB 6.0.2 / Redis 8.10.2)

Image `falkordb/falkordb@sha256:107455e862a824e45e8e9f5a7fbfdf5e00a08b14e13eae2577c72fd9e3f59ba6`.
`CALL dbms.procedures()` is the authority, and it disagrees with the documentation in four places
that matter:

| Finding | Docs say | Server does |
|---|---|---|
| BFS name | `algo.bfs` on the BFS page, `algo.BFS` in the procedure table | Both work — **procedure names are case-insensitive**. `dbms.procedures()` reports `algo.BFS`; we use that spelling for consistency with the registry. |
| BFS unlimited depth | `max_depth: 0` means unlimited | **0 returns nothing.** Depth must be a positive integer. |
| BFS source node | unstated | **Excluded** from `nodes`, so count is `size(nodes)`. |
| Vector `score` | example orders `DESC` | **`score` is a distance** — exact match ≈ 1e-10. Nearest is `ORDER BY score ASC`. |

Each of these fails *silently*: an empty blast radius, or recall ranked backwards. All were found
by running the queries rather than trusting the page, which is why the Day 1 exit criterion is
hand-verified traversals before any agent code exists.

Two further findings from writing the seeder, both of which change how every traversal is
written:

| Finding | Effect |
|---|---|
| A named variable-length relationship (`[deps:DEPENDS_ON*1..3]`) binds to a **Path**, not a list | `ALL(d IN deps …)` and `size(deps)` both raise `Type mismatch: expected List but was Path`. The working idiom is `ALL(d IN relationships(deps) …)` and `length(deps)`. |
| Variable-length **bounds cannot be parameterised** | `*1..$hops` raises `Encountered unhandled type in inlined properties`. A depth argument must be interpolated into the query string as a validated integer. |

Every one of these is pinned as a regression test in `tests/test_falkordb_semantics.py`, so an
image upgrade that changes the behaviour breaks a test rather than the demo.

Procedures actually registered: `algo.BFS`, `algo.SPpaths`, `algo.SSpaths`, `algo.WCC`,
`algo.MSF`, `algo.betweenness`, `algo.pageRank`, `algo.labelPropagation`,
`algo.HarmonicCentrality`, `algo.maxFlow`, plus the `db.*` utilities. Note there is **no**
`algo.CDLP` (it is `algo.labelPropagation`) and **no** A* procedure despite the algorithms index
listing A* search.

Argument styles are not uniform: `algo.BFS` is positional and rejects a config map
(`Received 1 arguments to procedure 'algo.BFS', expected at least 3`), while `algo.WCC` and
`algo.SPpaths` take config maps.

Confirmed working as documented: `algo.SPpaths` with `weightProp` (returns the path object and
its weight), `algo.WCC` with `{nodeLabels: [...]}`, vector index creation with
`OPTIONS {dimension, similarityFunction}`, `vecf32()` insertion, and
`db.idx.vector.queryNodes`.

Python client `FalkorDB` 1.7.1: `db.select_graph(name)`, `graph.query(q, params=...)`,
`graph.ro_query(...)`, and helpers `create_node_range_index`, `create_node_unique_constraint`,
`create_node_vector_index(label, *props, dim=, similarity_function=)`, `list_indices`,
`list_constraints`, `call_procedure`, `explain`, `profile`.

## Algorithms available as procedures

Confirmed present: `algo.pageRank`, `algo.BFS`, `algo.MSF`, `algo.WCC`, `algo.betweenness`,
`algo.labelPropagation`, `algo.SPpaths`, `algo.SSpaths`. The algorithms index additionally lists
A* search, harmonic centrality and MaxFlow.

**Naming discrepancy, resolved:** the procedures table spells it `algo.BFS` and the BFS page
`algo.bfs`. Tested: procedure names are case-insensitive, so both work. We use `algo.BFS`,
matching `dbms.procedures()`.

What we ended up using, and where (full table in `queries.md`, which is checked against the
code by a test):

- `algo.betweenness` → chokepoint scores, computed once at seed time and stored on `Service`.
- `algo.WCC` → fixture sanity check: the seeded mesh must be a single component.
- `algo.BFS` → in `verify.py`, to cross-check the variable-length traversal and to pin the
  depth-0 behaviour as a regression test.

Two procedures we tested, documented, and then did **not** use in the product:

- `algo.BFS` for blast radius. It was the original design. BFS follows edges in their stored
  direction, and with `(caller)-[:DEPENDS_ON]->(callee)` that walks away from the callers — it
  answers "what does this depend on", the opposite of the question. It also yields no per-node
  depth. A variable-length pattern match is the right primitive and gives depth for free.
- `algo.SPpaths` for a weighted culprit→victim path. Confirmed working (config map with
  `sourceNode`, `targetNode`, `relTypes`, `weightProp`, optional `costProp`/`maxCost`/`maxLen`/
  `relDirection`/`pathCount`), and it would draw a nice line on the demo page. It is not called
  anywhere in the product. Listed here and in `queries.md` as designed-not-implemented rather
  than left in a table that implies otherwise.

## Vector index

Confirmed: HNSW, cosine or euclidean, 1–4096 dimensions.

```cypher
CREATE VECTOR INDEX FOR (p:Plan) ON (p.embedding)
  OPTIONS {dimension: 384, similarityFunction: 'cosine', M: 16, efConstruction: 200}
```

Vectors are inserted with `vecf32([...])` and queried with
`CALL db.idx.vector.queryNodes('Plan', 'embedding', k, vecf32([...])) YIELD node, score`.

Two documented constraints that shape the design:

1. **"No support for filtering" — vector queries do not combine well with property filters.** So
   the `SUPERSEDED_BY` exclusion in vector recall may have to happen in Python after the `YIELD`
   rather than as a `WHERE` on the procedure's output. Planned for; it is a two-line difference.
2. All vectors in an index must match the declared dimension exactly.

**Decision: 384 dimensions**, cosine, from a local sentence-transformer. Reasons: normalized
embeddings suit cosine; 384 keeps the index small; and the embedding is only the *secondary*
recall path, so its quality is not load-bearing. Signature matching is primary precisely because
it is exact, cheap and explainable — the vector index is a near-miss net, and the submission says
so rather than overselling it.

**No API-dependent embeddings.** A local model means the demo runs without network for the
retrieval half, and the cost ledger stays attributable to the agent rather than mixing in
embedding calls.

## Index types

Range (string, numeric, geospatial), full-text (RediSearch-backed), vector (HNSW), and CCH
(weighted point-to-point routing). `CALL db.indexes()` lists them with type and status;
`CALL db.constraints()` lists UNIQUE/MANDATORY constraints.

CCH is for road-network-style weighted routing between arbitrary node pairs. Our graph is 40
nodes and the path queries are one-off, so CCH buys nothing — noted here so the choice is
deliberate rather than an oversight if a judge asks.

## Cypher limitations that affect us

1. **Relationship uniqueness.** When a relationship in a pattern is not referenced elsewhere in
   the query, FalkorDB verifies only that *at least one* matching relationship exists, so counts
   over such patterns are wrong. Every aggregation in `queries.md` references its edge alias
   (`WHERE ID(e) >= 0`). This is the single most likely source of a wrong-but-plausible number in
   this project.
2. **`LIMIT` does not constrain eager operations.** `CREATE`, `SET`, `DELETE`, `MERGE` and
   aggregating projections run fully before `LIMIT` applies. The write-back must never rely on
   `LIMIT` to bound what it writes; `record_outcome` gets an idempotency test instead.
3. **Indexes do not optimize `<>`.** No query filters on not-equal over an indexed property.
4. **No aggregations inside pattern comprehensions.** Aggregate in a preceding `WITH`, or use
   `size()` over a list comprehension.
5. **`shortestPath` is legal only in a `WITH` or `RETURN` clause**, not as a `MATCH` pattern.
   `MATCH p = shortestPath((a)-[:D*1..5]->(b))` — the spelling every Cypher example uses — fails
   with "FalkorDB currently only supports shortestPaths in WITH or RETURN clauses". Write
   `WITH shortestPath((a)-[:D*1..5]->(b)) AS p` instead. The confidence distance term needs it.
6. **A node is not at distance 0 from itself** under `*1..n`: `shortestPath` returns an empty
   path and a `NULL` length rather than a one-node path. `*0..n` returns the node at length 0.
   `confidence_facts` therefore decides "the deploy touched the failing service" in Python and
   only asks the graph about genuinely upstream deploys.

## Stack

| Choice | Decision | Why |
|---|---|---|
| Graph | FalkorDB, Docker, pinned by digest at first pull on Day 1 | Rule 2/4. Pinning keeps the demo reproducible for judges. |
| Client | `falkordb` Python package | Official. |
| Model | `claude-sonnet-5`, adaptive thinking | `budget_tokens` does not exist on it; effort is the cost lever. Moved from `claude-opus-5` for cost — the cold/warm ratio survived the change, which is why the claim is a ratio. |
| Agent loop | `client.beta.messages.tool_runner` with `@beta_tool` | No reason to hand-roll the loop; per-turn hooks cover approval and usage capture. |
| Embeddings | deterministic 384-dim unit vector derived from the signature (`seed._plan_embedding`) | Placeholder, and labelled as one in the code. The vector index is the *secondary*, near-miss recall path; signature collision is the primary one and needs no embedding. A real local sentence-transformer is the drop-in, and the dimension and similarity function are already the real ones. |
| MCP | `mcp` package, stdio, reflecting over the same registry | Track 01 names MCP tool selection. One registry, two transports. |
| Frontend | single HTML page, no framework | It must work in a video on Day 4, not scale. |

## Submission checklist (rule 10)

- [ ] public repo
- [ ] README with setup instructions **and** the graph data model → from `graph-model.md`
- [ ] the Cypher queries / graph algorithms the product depends on → from `queries.md`
- [ ] demo video
- [ ] live deployment, or complete local setup instructions
- [ ] tracks named: **Agents That Act on Connected Data** + **Agent Memory and Coordination**
- [ ] AI-assistance disclosure (rule 11) — Claude Code was used; state where and for what
- [ ] `walkthrough.md` current, so every query can be explained on demand (rules 12–13)
