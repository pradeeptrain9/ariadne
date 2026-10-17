# Graph data model

One FalkorDB graph named `ariadne`. Two subgraphs that meet at `Incident`: the **infrastructure**
subgraph the agent reasons over, and the **procedural memory** subgraph the agent writes back to.

```
          ┌──────────────── infrastructure ────────────────┐
          │                                                │
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
          ┌──────────────── procedural memory ─────────────┐
          │   Plan ─STEP→ Step ─USED_TOOL→ Tool            │
          │     │           └──OUTCOME→ Result             │
          │     ├─SIMILAR_TO→ Plan                         │
          │     └─SUPERSEDED_BY→ Plan                      │
          └────────────────────────────────────────────────┘
```

## Infrastructure subgraph

### Nodes

| Label | Properties | Notes |
|---|---|---|
| `Service` | `name` (unique), `tier` (1–3), `repo`, `paths` (list of code paths it owns) | `tier` 1 = customer-facing. Used to rank upstream candidates. |
| `Deploy` | `id` (unique), `at` (epoch ms), `sha`, `env` | One deploy touches one or more services. |
| `Commit` | `sha` (unique), `msg`, `files` (list), `at` | `files` is what makes culprit ranking computable rather than guessed. |
| `Person` | `handle` (unique), `name`, `tz` | |
| `Alert` | `id` (unique), `at`, `metric`, `value`, `severity` | `metric` is a controlled vocabulary (`latency_p99`, `error_rate`, `saturation`, `queue_depth`) so signatures are stable. |
| `FailureMode` | `name` (unique), `symptom` | e.g. `connection_pool_exhaustion`. |
| `Runbook` | `id` (unique), `title`, `url` | |
| `Incident` | `id` (unique), `at`, `status`, `signature` | The join point between the two subgraphs. |

### Edges

| Edge | Properties | Why it exists |
|---|---|---|
| `(Service)-[:DEPENDS_ON]->(Service)` | `critical` (bool), `calls_per_min` | The spine. Reverse traversal finds root-cause candidates; forward traversal is the blast radius. |
| `(Deploy)-[:TOUCHED]->(Service)` | — | Narrows "what changed" to a time window. |
| `(Commit)-[:IN]->(Deploy)` | — | |
| `(Commit)-[:AUTHORED_BY]->(Person)` | — | |
| `(Person)-[:OWNS]->(Service)` | `rotation` (`primary`/`secondary`) | Who to page. |
| `(Alert)-[:FIRED_ON]->(Service)` | — | Entry point for every investigation. |
| `(Service)-[:EXHIBITS]->(FailureMode)` | `seen_count` | Links a symptom to a known class of failure. |
| `(Runbook)-[:FIXES]->(FailureMode)` | — | Turns a diagnosis into an action. |
| `(Incident)-[:TRIGGERED_BY]->(Alert)` | — | |
| `(Incident)-[:CAUSED_BY]->(Commit)` | `confidence` (0–1), `model_confidence` (0–1), `overlap`, `files_changed`, `distance_hops`, `verified` (bool) | The verdict, stored. `confidence` is computed by `scoring.confidence` from the three terms stored beside it, so the number is auditable from the edge alone; `model_confidence` is what the model guessed and is used for nothing. `verified` separates "the agent said so" from "a human confirmed it". |
| `(Incident)-[:IMPACTED]->(Service)` | `depth` | Blast radius, stored at resolution time. |

## Procedural memory subgraph

| Label | Properties |
|---|---|
| `Plan` | `id`, `signature`, `runs`, `successes`, `mean_tokens`, `mean_cost_usd`, `embedding` (vector) |
| `Step` | `id`, `order`, `intent`, `tool_name`, `args_shape` |
| `Tool` | `name` (unique), `kind` (`read`/`write`), `mean_ms` |
| `Result` | `id`, `ok`, `tokens`, `ms`, `rows` |
| `Condition` | `name` (unique), `description` |

| Edge | Properties |
|---|---|
| `(Incident)-[:SOLVED_BY]->(Plan)` | `at` |
| `(Plan)-[:STEP]->(Step)` | `order` |
| `(Step)-[:USED_TOOL]->(Tool)` | — |
| `(Step)-[:OUTCOME]->(Result)` | — |
| `(Plan)-[:SIMILAR_TO]->(Plan)` | `score` |
| `(Plan)-[:SUPERSEDED_BY]->(Plan)` | `reason` |
| `(Tool)-[:FAILS_ON]->(Condition)` | `observed_count` |

### `Plan.signature` — the core idea

The signature is a **structural** fingerprint of the alert, not its text. Computed in Python,
deterministically, before the model is called:

```
signature = sha1(
    metric_kind,                  # latency_p99 | error_rate | saturation | queue_depth
    service_tier,                 # 1 | 2 | 3
    fan_in_bucket,                # 0 | 1-3 | 4-10 | 11+   (count of DEPENDS_ON into it)
    fan_out_bucket,               # same buckets, outgoing
    deploy_in_window,             # bool: any Deploy TOUCHED an upstream in the last 2h
    severity,
)[:12]
```

Two incidents on *completely different services* collide on signature when they have the same
shape — a tier-1 leaf with high fan-in, latency alert, recent upstream deploy. That collision is
the retrieval, and it is something a vector store over alert text cannot do: the alert texts are
not similar, the situations are.

Why computed and not asked of the model: a signature the model invents is unstable across runs,
which silently destroys the cache-hit rate that the whole Track 02 claim rests on.

`Plan.embedding` is the **secondary** recall path for near-misses — plans whose signature differs
by one bucket. Graph signature first (exact, cheap, explainable), vector second (fuzzy), union
the candidates, rank by `successes / runs`.

## Indexes and constraints

Planned (exact syntax verified against docs in `decisions.md`):

```
CREATE CONSTRAINT ... -- unique on Service.name, Commit.sha, Deploy.id, Alert.id,
                      -- Person.handle, Incident.id, Plan.id, Tool.name
CREATE INDEX FOR (d:Deploy) ON (d.at)        -- time-window queries
CREATE INDEX FOR (a:Alert)  ON (a.at)
CREATE INDEX FOR (p:Plan)   ON (p.signature) -- the memory lookup; must be indexed
CREATE INDEX FOR (s:Service) ON (s.tier)
CREATE VECTOR INDEX FOR (p:Plan) ON (p.embedding)
  OPTIONS {dimension: 384, similarityFunction: 'cosine', M: 16, efConstruction: 200}
```

Note from the FalkorDB docs: range indexes do **not** optimize `<>` filters, so no query in
`queries.md` relies on a not-equal filter over an indexed property.
