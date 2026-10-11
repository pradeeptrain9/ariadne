"""The agent loop.

Claude chooses which tool to call and writes the explanation. It does not rank, score or decide
what caused the incident — `suspect_commits` returns a computed file overlap, `scoring.py`
computes the confidence, and the traversals decide what is upstream and what breaks downstream.

Three things here are deliberately *not* the model's decision:

* the signature (`signature.py`) — a model-generated key would drift between runs and both plan
  recall and prompt caching depend on byte-stability;
* cold vs warm, and therefore the effort level — decided by a pre-flight memory lookup in
  Python, because "did we already solve this shape" is a graph fact;
* the verdict's structured form — it is whatever the model passed to `record_outcome`, so the
  machine-readable output and the graph write-back cannot disagree.

Prompt layout follows the render order `tools` → `system` → `messages`. Tool definitions and the
system prompt are the stable prefix and carry the cache breakpoints; the incident briefing varies
per request and goes last, after them. `verify_caching` asserts this actually works, because a
stray timestamp in the prefix would silently cost the Track 02 number.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

import anthropic

from . import tools as T
from .graph import Ariadne
from .ledger import Run, LEDGER_PATH
from .scoring import explain
from .signature import signature_of

MODEL = "claude-opus-5"
MAX_TOKENS = 16_000
MAX_ITERATIONS = 24

#: Effort by mode. A replay follows a sequence that already worked, so low effort is *correct*
#: there, not merely cheaper. A cold investigation gets the full budget.
EFFORT = {"cold": "high", "warm": "low"}

SYSTEM = """You are an on-call incident analyst. You answer three questions about an alert:
what changed, what else breaks, and who owns it.

Your context is a graph. Every tool is a traversal over it, and the traversals have already done
the ranking:

- `upstream_candidates` returns dependencies ordered by criticality, distance and tier.
- `suspect_commits` returns an `overlap` count: how many of a commit's changed files fall under
  the failing service's owned path prefixes.
- `blast_radius` returns the complete set of transitive callers, with depth.

Those numbers are facts. Do not re-rank them by reading commit messages, and do not describe a
commit as the likely cause because its message sounds relevant. If you want to disagree with the
ranking, say which number you are disagreeing with.

How to work:

1. If the briefing says memory HIT, call `recall_plan` once. It returns a hypothesis about where
   the cause lived last time, plus the current blast radius, owner and runbook — all recomputed,
   none of it stale. Your only remaining job is to verify the hypothesis: call
   `deploys_in_window` for the failing service alone, then `suspect_commits`, then record.
   Do not call `upstream_candidates`, `blast_radius`, `owner_of` or `runbook_for` on a hit —
   their answers are already in the recall result, and re-deriving them is the cost the memory
   exists to avoid. If the verification fails, say so and investigate properly.
2. If the briefing says memory MISS, investigate: narrow the dependency cone, find what shipped
   in the window, rank the commits, measure the blast radius, find the owner and the runbook.
3. An empty `deploys_in_window` is an answer, not a dead end. It means nothing shipped, so the
   cause is structural — a saturated or already-degraded dependency. Say that, and do not
   nominate a commit anyway.
4. A commit with `overlap: 0` that changes `libs/common/...` can still be the cause. The overlap
   ranking scores those low. If you suspect one, say so explicitly and explain why you are going
   against the ranking.
5. Finish by calling `record_outcome`. Pass `culprit_sha: null` when no commit is responsible.

Then write the verdict as a handful of short bullets — no headings, no tables, no preamble.
Cover: the culprit (or that there isn't one), the confidence and what it was computed from, the
impacted services, the owner to page, and the runbook. Be specific and brief. State what you did
not establish rather than filling the gap with a plausible sentence."""


def briefing(alert: dict[str, Any], signature: str, shape: Any, memory: dict[str, Any]) -> str:
    """The per-incident message. Everything volatile lives here, after the cache breakpoints."""
    if memory.get("found"):
        mem = (
            f"memory HIT — plan {memory['plan']} has run {memory['runs']} time(s) with "
            f"{memory['successes']} success(es).\n"
            f"Hypothesis from the graph: {memory['hypothesis']}.\n"
            f"Call recall_plan once to get that plus the current impact, owner and runbook, then "
            f"verify the hypothesis with deploys_in_window on `{alert['service']}` alone and "
            f"suspect_commits. Skip the other read tools."
        )
    else:
        mem = "memory MISS — this shape has not been seen. Investigate from scratch."

    return f"""Alert {alert['id']} fired on service `{alert['service']}`.

  metric:    {alert['metric']} = {alert['value']}
  severity:  {alert['severity']}
  timestamp: {alert['at']} (epoch ms)

Structural signature: {signature}
  {json.dumps(shape.as_dict())}

{mem}

Investigate and record the outcome. Use incident id "inc-{alert['id']}"."""


def preflight(a: Ariadne, signature: str, service: str) -> dict[str, Any]:
    """Is this shape already in memory?

    Run in Python, before the model sees anything, because the answer decides the effort level
    for the whole run. Letting the model decide its own budget would make the cold/warm
    comparison a model preference rather than a measurement.
    """
    with T.bind(a):  # unrecorded: this lookup is ours, not one of the agent's steps
        return T.recall_plan(signature, service)


def investigate(
    a: Ariadne,
    alert: dict[str, Any],
    *,
    client: anthropic.Anthropic | None = None,
    ledger: Path | None = LEDGER_PATH,
    force_mode: str | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    verbose: bool = True,
) -> tuple[Run, str]:
    """Run one investigation. Returns the ledger Run and the model's final text.

    `force_mode="cold"` withholds memory from the briefing even when a plan exists. That is what
    makes the cost comparison honest: the same alert, against the same graph, investigated with
    memory and without it. Comparing a warm run on one alert to a cold run on a *different*
    alert would measure the alerts, not the memory.
    """
    client = client or anthropic.Anthropic()

    signature, shape = signature_of(a, alert)
    memory = preflight(a, signature, alert["service"])
    if force_mode == "cold":
        memory = {"found": False, "signature": signature,
                  "note": "memory withheld for this measurement run"}
    mode = force_mode or ("warm" if memory.get("found") else "cold")
    effort = EFFORT[mode]

    run = Run(alert_id=alert["id"], signature=signature, mode=mode, effort=effort)

    def emit(kind: str, payload: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(kind, payload)

    if verbose:
        print(f"alert {alert['id']} on {alert['service']}  sig={signature}  "
              f"mode={mode}  effort={effort}")
    emit("start", {
        "alert": alert, "signature": signature, "shape": shape.as_dict(),
        "mode": mode, "effort": effort,
        "memory": {"found": bool(memory.get("found")),
                   "plan": memory.get("plan"),
                   "runs": memory.get("runs"),
                   "hypothesis": memory.get("hypothesis")},
    })

    final_text = ""
    with T.bind(a, run):
        runner = client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            max_iterations=MAX_ITERATIONS,
            system=[{
                "type": "text",
                "text": SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            output_config={"effort": effort},
            tools=T.TOOLS,
            messages=[{
                "role": "user",
                "content": briefing(alert, signature, shape, memory),
            }],
        )
        for message in runner:
            run.add_usage(message.usage)
            emit("usage", {
                "turns": run.turns, "tokens": run.tokens, "cache_read": run.cache_read,
                "cost": round(run.cost, 6), "tool_calls": run.tool_calls,
            })
            for block in message.content:
                if block.type == "text" and block.text.strip():
                    final_text = block.text
                    if verbose:
                        print(f"  · {block.text.strip()[:160]}")
                    emit("text", {"text": block.text})
                elif block.type == "tool_use":
                    if verbose:
                        print(f"  → {block.name}({json.dumps(block.input)[:110]})")
                    emit("tool", {"name": block.name, "input": block.input,
                                  "rows": run.calls[-1].rows if run.calls else None,
                                  "ms": run.calls[-1].ms if run.calls else None})

    run.finish()
    _attach_verdict(a, run, alert)
    if ledger:
        run.append_to(ledger)
    if verbose:
        print(run.summary())
    emit("done", {"run": run.as_dict(), "text": final_text})
    return run, final_text


def _attach_verdict(a: Ariadne, run: Run, alert: dict[str, Any]) -> None:
    """Read back what the agent wrote, so the ledger records the graph's version of the verdict
    rather than the model's prose."""
    row = a.one("""
        MATCH (i:Incident {id: $iid})
        OPTIONAL MATCH (i)-[cb:CAUSED_BY]->(c:Commit)
        OPTIONAL MATCH (i)-[im:IMPACTED]->(s:Service) WHERE ID(im) >= 0
        RETURN c.sha AS culprit, cb.confidence AS confidence, count(s) AS impacted
    """, {"iid": f"inc-{alert['id']}"})
    if row:
        run.culprit = row["culprit"]
        run.confidence = float(row["confidence"]) if row["confidence"] is not None else None
        run.impacted = int(row["impacted"] or 0)


def verify_caching(client: anthropic.Anthropic | None = None) -> dict[str, int]:
    """Prove the cache prefix works.

    Two identical minimal requests: the second must report cache reads. If it does not, a
    silent invalidator is in the prefix and every cost number downstream is wrong.
    """
    client = client or anthropic.Anthropic()
    usage = []
    for _ in range(2):
        msg = client.beta.messages.create(
            model=MODEL,
            max_tokens=64,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=[t.to_dict() if hasattr(t, "to_dict") else dict(t) for t in T.TOOLS],
            messages=[{"role": "user", "content": "Reply with the single word: ready."}],
        )
        usage.append({
            "input": msg.usage.input_tokens,
            "cache_write": msg.usage.cache_creation_input_tokens or 0,
            "cache_read": msg.usage.cache_read_input_tokens or 0,
        })
    return {"first": usage[0], "second": usage[1]}


def main() -> None:
    ap = argparse.ArgumentParser(description="Investigate one alert.")
    ap.add_argument("--alert", help="alert id from the graph; defaults to scenario A's first")
    ap.add_argument("--scenario", choices=["a1", "a2", "b"], help="use a held-back demo alert")
    ap.add_argument("--check-cache", action="store_true", help="prove prompt caching works")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.check_cache:
        print(json.dumps(verify_caching(), indent=2))
        return

    scenarios = json.loads(Path("fixtures/scenarios.json").read_text())
    alert: dict[str, Any] | None = None
    if args.scenario:
        alert = {"a1": scenarios["scenario_a"][0], "a2": scenarios["scenario_a"][1],
                 "b": scenarios["scenario_b"]}[args.scenario]

    with Ariadne() as a:
        if alert is None and args.alert:
            alert = a.one("""MATCH (x:Alert {id: $id})-[:FIRED_ON]->(s:Service)
                             RETURN x.id AS id, x.at AS at, x.metric AS metric,
                                    x.value AS value, x.severity AS severity,
                                    s.name AS service""", {"id": args.alert})
            if alert is None:
                raise SystemExit(f"no such alert: {args.alert}")
        if alert is None:
            alert = scenarios["scenario_a"][0]

        run, text = investigate(a, alert, verbose=not args.quiet)
        print()
        print(text.strip())


if __name__ == "__main__":
    main()
