# Ariadne — design notes

Pre-window design work for **Graph Hacks** (FalkorDB, Oct 15–18 2026).

Rule 8 of the hackathon rules: *"The main coding and design work must begin after the hackathon
starts. You may discuss ideas, create notes, sketch the graph model, or prepare diagrams
beforehand."* Everything in this directory is notes, model sketches and diagrams, first written
2026-10-11. **Code was also written before the window opened** — the graph, seed and
verification code in `ariadne/` date from Oct 11–12, earlier than rule 8 intends. That is
disclosed in the top-level README and SUBMISSION.md, and the commits carry their real dates. The
Cypher in `queries.md` now matches the code, and `tests/test_docs_match_code.py` pins it.

## What Ariadne is

An on-call agent that takes an alert and answers the three questions on-call asks
first: *what changed, what else breaks, who owns it.* The answer is a path through a
FalkorDB graph of services, deploys, commits and people — so the explanation is the artifact,
not a narration bolted onto a score.

Every resolved incident is written back as a reusable **plan** subgraph. The next alert with the
same *structural shape* retrieves that plan instead of re-investigating, which makes the second
answer measurably cheaper than the first — 1.6× on `claude-sonnet-5` over 20 alerts, see `measurements/`. That is the Track 02 entry; the
investigation itself is the Track 01 entry. One codebase, two tracks (rule 5 allows this
explicitly).

## The design rule

**Ranking is computed, never asked of a model.** Cypher traversal and FalkorDB's graph
algorithms decide which commit is suspect and what the blast radius is. Claude decides which
tool to call and writes the prose. Remove the model and the answer is still correct — it just
reads worse. Remove the graph and there is no product at all (rule 3).

## Documents

| File | Contents |
|---|---|
| `graph-model.md` | Nodes, edges, properties, indexes, and why each exists |
| `queries.md` | The traversal behind each agent tool, as Cypher sketches |
| `architecture.md` | Agent loop, memory lifecycle, cost ledger, MCP surface |
| `fixtures.md` | Synthetic data spec and the three scenario classes |
| `decisions.md` | FalkorDB capabilities confirmed from docs, and what we pin |
| `walkthrough.md` | Explain-it-to-a-judge notes (started Day 1, kept current) |
