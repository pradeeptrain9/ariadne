# Architecture

Python 3.11. FalkorDB in Docker. `anthropic` SDK with the beta tool runner. One tool registry
served over two transports (in-process for the agent, MCP stdio for external clients).

```
  alert JSON
      │
      ▼
 signature.py ──── deterministic structural fingerprint (no model involved)
      │
      ▼
  agent.py ─── client.beta.messages.tool_runner(model="claude-sonnet-5", tools=REGISTRY)
      │                    │
      │                    ├── recall_plan ──────┐
      │                    ├── upstream_candidates│
      │                    ├── deploys_in_window  │  all thin wrappers
      │                    ├── suspect_commits    ├─ over Cypher in
      │                    ├── blast_radius       │  queries.md
      │                    ├── owner_of           │
      │                    ├── runbook_for        │
      │                    ├── prior_incidents    │
      │                    └── record_outcome ────┘
      │                                  │
      ▼                                  ▼
  verdict + path                  FalkorDB (ariadne graph)
      │
      ▼
  ledger.py ──→ runs.jsonl ──→ the cold-vs-warm chart
```

## The loop

1. **Signature.** `signature.py` computes the structural fingerprint from the alert plus two
   cheap graph lookups (service tier, fan-in/fan-out buckets). Deterministic and model-free —
   this is what makes plan recall and prompt caching stable.

2. **Recall.** `recall_plan` is always the first tool call. The system prompt instructs the model
   to call it before anything else, and the tool's own description says so too.
   - **Hit** → replay: the model executes the stored step sequence, verifies the culprit still
     holds (`suspect_commits` on the current window), and answers. Target: 3–5 tool calls.
   - **Miss** → investigate: `upstream_candidates` → `deploys_in_window` → `suspect_commits` →
     `blast_radius` → `owner_of` → `runbook_for`. Target: 10–15 tool calls.

3. **Verdict.** Structured output: culprit sha, confidence, impacted services with depth, owner
   handle, runbook, and the path as an ordered node list. The path is rendered, not narrated.

4. **Write-back.** `record_outcome` stores the incident and the plan. Success increments
   `Plan.successes`; a rejected verdict supersedes the plan and marks `FAILS_ON` on the tool that
   led it astray.

### Confidence, written down once

```
confidence = clamp(0.1, 0.95,
      0.55 * (overlap / max(1, files_changed))      # file-path overlap, computed
    + 0.25 * (1 / distance)                          # hops from failing service
    + 0.20 * (1 if deploy_within_30min else 0.4)     # temporal proximity
)
```

Every term comes from a Cypher result, and the number is computed **at write time**, in
`tools.computed_confidence`, not taken from the model. `record_outcome` recomputes it from the
graph, stores it on `CAUSED_BY` together with its terms (`overlap`, `files_changed`,
`distance_hops`), and returns it so the written verdict quotes it. The model's own estimate is
stored beside it as `model_confidence` and is used for nothing — it is there so the two can be
compared.

This was wrong until Day 2: the model passed a confidence and `record_outcome` wrote it straight
through, so the one number in the verdict that reads most like a measurement was the only one
the model had invented. The seeded history had the same hole — 68 historical confidences from
`rng.uniform(0.62, 0.93)`, handed to the model as evidence by `recall_plan`. Both now come from
the formula above.

Asked "where does 0.78 come from", the answer is the three numbers that produced it, and they
are on the edge.

## Cost discipline

This is the Track 02 deliverable, not hygiene. Three levers, all measured:

**Prompt caching.** Render order is `tools` → `system` → `messages`, so the stable prefix is: the
tool definitions, then the system prompt, then a frozen block containing the graph schema and the
Cypher contract. `cache_control: {type: "ephemeral"}` goes at the end of that block. The alert
payload and the signature go *after* it — they vary per request and would otherwise invalidate
everything. A test asserts `usage.cache_read_input_tokens > 0` on the second call of a session;
a silent invalidator (a timestamp in the system prompt, an unsorted tool list) would quietly
destroy the headline number, so it is checked rather than assumed.

**Effort.** `output_config: {effort: "high"}` on a cold investigation, `"low"` on a plan replay.
A replay is following a known sequence, not reasoning from scratch — low effort is correct there,
not merely cheaper. Both levers appear in the chart legend so the saving is attributed honestly:
part memory, part effort.

**Ledger.** Every turn's `usage` is appended to `runs.jsonl`: input, output, cache read, cache
write, tool count, wall ms, mode (cold/warm), model, incident id, and cost computed from that model's own prices
in `ledger.PRICES` (Sonnet 5: $2/$10 per MTok). The model is recorded per run, and
`ledger.compare` refuses to present a ratio from a ledger that mixes models. The chart is generated from that file. No number in the
submission is typed by hand.

Model: `claude-sonnet-5`, adaptive thinking (the default; `budget_tokens` does not exist on this
model). Streaming for the investigation path so the demo can show tool calls arriving live.

## Memory lifecycle

| Event | Graph effect |
|---|---|
| New shape resolved | `Plan` created, `runs = 1`, steps recorded |
| Same shape, replay succeeds | `runs += 1`, `successes += 1`, rolling mean cost updated |
| Same shape, replay reaches wrong culprit | `SUPERSEDED_BY` → new plan; `Tool-[:FAILS_ON]->Condition` incremented |
| Near-miss shape | vector recall finds the plan, `SIMILAR_TO` edge written with its score |

`Plan.mean_cost_usd` is a rolling mean maintained in Cypher, so "this plan costs $0.06" is a
property of the graph a judge can query directly rather than a claim in a README.

## MCP surface

`mcp_server.py` exposes the same registry over stdio. Two reasons, both load-bearing:

1. Track 01 names "tool selection through MCP" as a focus area; this satisfies it with the real
   tools rather than a wrapper written for the judges.
2. The demo video can show Claude Desktop answering an on-call question against the same graph,
   which proves the tools are not welded to our agent loop.

One registry, two transports. The tool bodies are never duplicated: `tools.py` holds the
functions, and `MCPServer.add_tool` derives each schema from the same signature and docstring the
Anthropic SDK reads. `tests/test_mcp_server.py` asserts the two surfaces agree on names,
parameters, required fields and guidance prose — the failure mode of "expose them over MCP too"
is a second copy that silently drifts.

Three things learned from making it actually speak the protocol:

- **Writes are off by default.** `record_outcome` mutates the memory graph, and a client holding
  a stdio pipe should not be able to reinforce or invent plans unless the operator opted in
  (`ARIADNE_MCP_ALLOW_WRITES=1`). The read-only tools are annotated `read_only_hint: true`.
- **A list return becomes one content block per row**, not a single JSON array. A client reading
  `content[0]` gets the first row and silently believes it is the whole answer.
- **Tool errors are reported generically** — a raised `ValueError` reaches the client as
  "Error executing tool", with the detail staying server-side. Good for not leaking internals,
  but it means argument validation messages are for our logs, not for the model. The validation
  itself still matters: `hops` is interpolated into Cypher, so it is checked before use.

The connection is opened lazily on the first tool call rather than at import, so launching the
server does not require FalkorDB to be up — a client that spawns the process gets a clear error
from the call instead of a crash at startup.

## Demo frontend

Single page, no framework. Three regions:

- **Mesh** — force-directed graph of the 40 services, dependency edges greyed out.
- **Transcript** — tool calls streaming in as they happen, each labelled with the rows returned.
- **Verdict + cost** — culprit card, impacted-services list, and a counter that ticks up as the
  ledger receives usage, beside the cold-vs-warm bars.

The path lighting up through the mesh is the thing a judge remembers. Build that before polish.

Fallback if Day 4 runs short: a terminal recording. Rule 10 requires a demo video and local setup
instructions, not a web app.
