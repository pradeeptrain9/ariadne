"""The agent's tools.

Every tool is a thin wrapper over Cypher: the graph does the reasoning, the tool returns rows.
Nothing here asks the model to rank, score or judge — `suspect_commits` returns a computed file
overlap, `upstream_candidates` returns a computed ordering. The model's job is choosing which
tool to call next and writing the explanation.

Tool *descriptions* are the real prompt surface, so each docstring states what the traversal
guarantees. That is what stops the model re-deriving in prose something the graph already
computed.

One registry, two transports: `TOOL_FUNCS` holds the plain callables (used by the tests and by
`mcp_server.py`) and `TOOLS` holds the same functions wrapped for the Anthropic tool runner. The
bodies are never duplicated.
"""

from __future__ import annotations

import functools
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from anthropic import beta_tool
from anthropic.lib.tools import ToolError

from .graph import Ariadne
from .ledger import Run, ToolCall
from .scoring import confidence as score_confidence
from .signature import DEPLOY_WINDOW_MS

MAX_HOPS = 5


@dataclass
class ToolContext:
    graph: Ariadne
    run: Run | None = None
    #: Called with each ToolCall once the tool body has returned. The demo's event stream uses
    #: it: row counts and latencies only exist *after* execution, so they cannot be read off the
    #: assistant message that requested the call.
    on_call: Callable[[ToolCall], None] | None = None
    _order: int = 0

    def next_order(self) -> int:
        """Position of the call about to be recorded."""
        return len(self.run.calls) if self.run is not None else self._order


_CTX: ContextVar[ToolContext | None] = ContextVar("ariadne_tool_ctx", default=None)


@contextmanager
def bind(
    graph: Ariadne,
    run: Run | None = None,
    on_call: Callable[[ToolCall], None] | None = None,
) -> Iterator[ToolContext]:
    """Make a graph (and optionally a ledger Run) visible to the tools.

    The graph cannot be a tool argument — the model must not be able to choose which database it
    talks to, and the generated tool schema should contain only things the model legitimately
    decides.

    `on_call` receives each completed ToolCall, which is how the demo streams a tool's real row
    count and duration.
    """
    ctx = ToolContext(graph=graph, run=run, on_call=on_call)
    token = _CTX.set(ctx)
    try:
        yield ctx
    finally:
        _CTX.reset(token)


def _ctx() -> ToolContext:
    ctx = _CTX.get()
    if ctx is None:
        raise RuntimeError("tool called outside ariadne.tools.bind(graph)")
    return ctx


def rows_result(rows: list[dict[str, Any]], *, empty_note: str | None = None) -> dict[str, Any]:
    """Wrap query rows for return to the model.

    Tools must not return a bare list: the Messages API requires each `tool_result` content entry
    to be an object, and a list of row dicts is rejected with
    `tool_result.content.0: Input should be an object`. Wrapping also lets an empty result carry
    its meaning — "nothing shipped" is an answer, and an empty array alone does not say so.
    """
    out: dict[str, Any] = {"count": len(rows), "rows": rows}
    if not rows and empty_note:
        out["note"] = empty_note
    return out


def _recorded(name: str, args: dict[str, Any], fn: Callable[[Ariadne], Any]) -> Any:
    """Run a tool body, timing it and recording it on the ledger Run."""
    ctx = _ctx()
    started = time.perf_counter()
    ok, error, out = True, None, None
    try:
        out = fn(ctx.graph)
        return out
    except Exception as exc:
        ok, error = False, str(exc)
        raise
    finally:
        if ctx.run is not None or ctx.on_call is not None:
            if isinstance(out, dict) and "rows" in out:
                rows = len(out["rows"])
            elif isinstance(out, list):
                rows = len(out)
            else:
                rows = 0 if out is None else 1
            call = ToolCall(
                order=ctx.next_order(), tool=name, args=args, rows=rows,
                ms=int((time.perf_counter() - started) * 1000), ok=ok, error=error,
            )
            ctx._order = call.order + 1
            if ctx.run is not None:
                ctx.run.add_call(call)
            if ctx.on_call is not None:
                ctx.on_call(call)


def _hops(n: int) -> int:
    """Validate a traversal depth.

    Variable-length bounds cannot be parameterised in FalkorDB (`*1..$n` raises), so the depth is
    interpolated into the query string — which means it must be proven to be a small integer
    first, not passed through from model output.
    """
    n = int(n)
    if not 1 <= n <= MAX_HOPS:
        raise ValueError(f"hops must be between 1 and {MAX_HOPS}, got {n}")
    return n


# ======================================================================================
# memory
# ======================================================================================

def recall_plan(signature: str, service: str) -> dict[str, Any]:
    """Look up what this situation's shape turned out to be last time. CALL THIS FIRST.

    On a hit this returns a complete prior verdict in one call: where the cause lived (the failing
    service's own deploy, or an upstream one), the typical file overlap, plus the *current*
    blast radius, owner and runbook for this service — recomputed now, not cached, so none of it
    is stale.

    That means a hit leaves you only one thing to establish: whether the same kind of cause is
    present in this window. Verify it with `deploys_in_window` on the failing service and
    `suspect_commits`, then record. Do not re-run `upstream_candidates`, `blast_radius`,
    `owner_of` or `runbook_for` — their answers are already below, and re-deriving them is the
    cost this memory exists to avoid.

    If the verification fails — no deploy in the window, or no commit with meaningful overlap —
    then the hypothesis does not hold here. Say so and investigate properly.

    Args:
        signature: the alert's structural signature, supplied in the incident briefing.
        service: the service the alert fired on.
    """
    def body(a: Ariadne) -> dict[str, Any]:
        plan = a.one("""
            MATCH (p:Plan {signature: $sig})
            OPTIONAL MATCH (p)-[sb:SUPERSEDED_BY]->(:Plan)
            WITH p, sb WHERE sb IS NULL
            RETURN p.id AS plan, p.runs AS runs, p.successes AS successes,
                   p.mean_cost_usd AS mean_cost_usd
            ORDER BY (toFloat(p.successes) / p.runs) DESC LIMIT 1
        """, {"sig": signature})
        if not plan:
            return {"found": False, "signature": signature,
                    "note": "no plan for this shape; investigate from scratch"}

        # Where did the cause live, across every prior incident of this shape? Computed, so the
        # hypothesis the agent verifies is a graph fact rather than a model recollection.
        cause = a.one("""
            MATCH (i:Incident {signature: $sig})-[cb:CAUSED_BY]->(c:Commit)
            WHERE ID(cb) >= 0
            MATCH (i)-[tb:TRIGGERED_BY]->(:Alert)-[fo:FIRED_ON]->(fs:Service)
            WHERE ID(tb) >= 0 AND ID(fo) >= 0
            MATCH (c)-[inr:IN]->(d:Deploy)-[t:TOUCHED]->(ds:Service)
            WHERE ID(inr) >= 0 AND ID(t) >= 0
            WITH count(DISTINCT i) AS incidents,
                 sum(CASE WHEN ds.name = fs.name THEN 1 ELSE 0 END) AS self_deploys
            RETURN incidents, self_deploys
        """, {"sig": signature})

        impact = a.q("""
            MATCH (s:Service {name: $service})
            MATCH (caller:Service)-[deps:DEPENDS_ON*1..5]->(s)
            WHERE ALL(d IN relationships(deps) WHERE ID(d) >= 0)
            RETURN caller.name AS service, caller.tier AS tier, min(length(deps)) AS depth
            ORDER BY tier ASC, depth ASC
        """, {"service": service})
        owner = a.q("""
            MATCH (p:Person)-[o:OWNS]->(s:Service {name: $service})
            WHERE ID(o) >= 0
            RETURN p.handle AS handle, p.name AS name, p.tz AS tz, o.rotation AS rotation
            ORDER BY CASE o.rotation WHEN 'primary' THEN 0 ELSE 1 END
        """, {"service": service})
        runbook = a.q("""
            MATCH (s:Service {name: $service})-[e:EXHIBITS]->(fm:FailureMode)<-[f:FIXES]-(r:Runbook)
            WHERE ID(e) >= 0 AND ID(f) >= 0
            RETURN r.title AS title, r.url AS url, fm.name AS failure_mode
            ORDER BY e.seen_count DESC LIMIT 1
        """, {"service": service})

        incidents = int((cause or {}).get("incidents") or 0)
        self_deploys = int((cause or {}).get("self_deploys") or 0)
        hypothesis = (
            f"in {self_deploys} of {incidents} prior incidents of this shape, the cause was a "
            f"commit in a deploy that touched the failing service itself"
            if incidents else "prior incidents of this shape recorded no commit cause"
        )

        return {
            "found": True,
            **plan,
            "hypothesis": hypothesis,
            "verify_with": ["deploys_in_window(services=[the failing service])",
                            "suspect_commits"],
            "already_answered": {
                "blast_radius": {"impacted_count": len(impact),
                                 "tier1_impacted": [r["service"] for r in impact
                                                    if int(r["tier"]) == 1],
                                 "impacted": impact},
                "owner": owner,
                "runbook": runbook[0] if runbook else None,
            },
        }

    return _recorded("recall_plan", {"signature": signature, "service": service}, body)


def prior_incidents(signature: str) -> dict[str, Any]:
    """Past incidents that had this exact structural signature, with their verified causes.

    Use this to check whether a culprit found now matches what actually caused the same shape of
    failure before. `verified: true` means a human confirmed it.

    Args:
        signature: the alert's structural signature.
    """
    def body(a: Ariadne) -> dict[str, Any]:
        return rows_result(a.q("""
            MATCH (i:Incident {signature: $sig})-[cb:CAUSED_BY]->(c:Commit)
            WHERE ID(cb) >= 0
            MATCH (i)-[tb:TRIGGERED_BY]->(x:Alert)-[fo:FIRED_ON]->(s:Service)
            WHERE ID(tb) >= 0 AND ID(fo) >= 0
            RETURN i.id AS incident, i.at AS at, s.name AS service, c.sha AS culprit,
                   c.msg AS commit_msg, cb.confidence AS confidence, cb.verified AS verified
            ORDER BY i.at DESC LIMIT 5
        """, {"sig": signature}),
            empty_note="no past incident has this signature")

    return _recorded("prior_incidents", {"signature": signature}, body)


# ======================================================================================
# investigation
# ======================================================================================

def upstream_candidates(service: str, hops: int = 3) -> dict[str, Any]:
    """Services the failing one depends on, transitively — the things that could have broken it.

    Already ranked by the graph: paths marked critical first, then fewest dependency hops, then
    lowest tier number, then highest betweenness (a precomputed chokepoint score). The ordering
    is computed, so take the top entries rather than re-reasoning about importance.

    Args:
        service: the service the alert fired on.
        hops: how many dependency hops to follow, 1 to 5. Default 3.
    """
    n = _hops(hops)

    def body(a: Ariadne) -> dict[str, Any]:
        return rows_result(a.q(f"""
            MATCH (failing:Service {{name: $service}})
            MATCH path = (failing)-[deps:DEPENDS_ON*1..{n}]->(cand:Service)
            WHERE ALL(d IN relationships(deps) WHERE ID(d) >= 0)
            WITH cand, min(length(deps)) AS distance,
                 max(CASE WHEN ANY(d IN relationships(deps) WHERE d.critical)
                          THEN 1 ELSE 0 END) AS on_critical_path
            RETURN cand.name AS service, cand.tier AS tier, distance, on_critical_path,
                   cand.betweenness AS betweenness
            ORDER BY on_critical_path DESC, distance ASC, tier ASC, betweenness DESC
            LIMIT 15
        """, {"service": service}),
            empty_note=f"{service} has no dependencies; it cannot have been broken by one")

    return _recorded("upstream_candidates", {"service": service, "hops": n}, body)


def deploys_in_window(services: list[str], alert_at: int) -> dict[str, Any]:
    """Deploys that touched any of these services in the two hours before the alert.

    An empty result is a real answer, not a failure: it means nothing shipped, so the cause is
    not a code change and you should look for a structural cause instead (saturation upstream,
    a dependency already degraded).

    Args:
        services: service names to check — typically the failing service plus its upstream
            candidates.
        alert_at: the alert timestamp in epoch milliseconds.
    """
    def body(a: Ariadne) -> dict[str, Any]:
        return rows_result(a.q("""
            UNWIND $services AS name
            MATCH (d:Deploy)-[t:TOUCHED]->(s:Service {name: name})
            WHERE ID(t) >= 0 AND d.at <= $at AND d.at >= $at - $window
            RETURN DISTINCT d.id AS deploy, d.sha AS sha, d.at AS at, s.name AS service,
                   ($at - d.at) / 60000 AS minutes_before_alert
            ORDER BY d.at DESC
        """, {"services": list(services), "at": int(alert_at), "window": DEPLOY_WINDOW_MS}),
            empty_note="nothing shipped in this window, so no commit can be the cause; "
                       "look for a structural cause instead")

    return _recorded("deploys_in_window",
                     {"services": list(services), "alert_at": int(alert_at)}, body)


def suspect_commits(deploy_ids: list[str], failing_service: str) -> dict[str, Any]:
    """Commits in those deploys, ranked by how much they touched the failing service's own code.

    `overlap` is a count of changed files falling under the failing service's owned path
    prefixes, computed by the graph. The ordering is overlap first, then recency. Do not re-rank
    by reading the commit messages — the message is prose and the overlap is a fact.

    A commit with `overlap: 0` that still looks suspicious is usually a shared-library change
    (`libs/common/...`). Those are real causes that this ranking scores low, so say so explicitly
    rather than silently promoting one.

    Args:
        deploy_ids: deploy ids from deploys_in_window.
        failing_service: the service the alert fired on.
    """
    def body(a: Ariadne) -> dict[str, Any]:
        return rows_result(a.q("""
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
        """, {"deploys": list(deploy_ids), "failing": failing_service}),
            empty_note="those deploys contain no commits")

    return _recorded("suspect_commits",
                     {"deploy_ids": list(deploy_ids), "failing_service": failing_service}, body)


def blast_radius(service: str) -> dict[str, Any]:
    """Everything that breaks when this service breaks — its transitive callers, with depth.

    Computed with a graph traversal, so the list is complete rather than a guess. Tier-1 entries
    are customer-facing and are what makes an incident urgent.

    Args:
        service: the service to measure impact from.
    """
    def body(a: Ariadne) -> dict[str, Any]:
        rows = a.q("""
            MATCH (s:Service {name: $service})
            MATCH (caller:Service)-[deps:DEPENDS_ON*1..5]->(s)
            WHERE ALL(d IN relationships(deps) WHERE ID(d) >= 0)
            RETURN caller.name AS service, caller.tier AS tier, min(length(deps)) AS depth
            ORDER BY tier ASC, depth ASC
        """, {"service": service})
        return {
            "service": service,
            "impacted_count": len(rows),
            "tier1_impacted": [r["service"] for r in rows if int(r["tier"]) == 1],
            "impacted": rows,
        }

    return _recorded("blast_radius", {"service": service}, body)


def owner_of(service: str) -> dict[str, Any]:
    """Who owns this service, primary on-call first.

    Args:
        service: the service to look up.
    """
    def body(a: Ariadne) -> dict[str, Any]:
        return rows_result(a.q("""
            MATCH (p:Person)-[o:OWNS]->(s:Service {name: $service})
            WHERE ID(o) >= 0
            RETURN p.handle AS handle, p.name AS name, p.tz AS tz, o.rotation AS rotation
            ORDER BY CASE o.rotation WHEN 'primary' THEN 0 ELSE 1 END
        """, {"service": service}),
            empty_note=f"{service} has no recorded owner")

    return _recorded("owner_of", {"service": service}, body)


def runbook_for(service: str) -> dict[str, Any]:
    """Runbooks for the failure modes this service is known to exhibit, most-seen first.

    Args:
        service: the service to look up.
    """
    def body(a: Ariadne) -> dict[str, Any]:
        return rows_result(a.q("""
            MATCH (s:Service {name: $service})-[e:EXHIBITS]->(fm:FailureMode)<-[f:FIXES]-(r:Runbook)
            WHERE ID(e) >= 0 AND ID(f) >= 0
            RETURN r.title AS title, r.url AS url, fm.name AS failure_mode,
                   fm.symptom AS symptom, e.seen_count AS seen
            ORDER BY e.seen_count DESC LIMIT 3
        """, {"service": service}),
            empty_note=f"no runbook is linked to {service}'s known failure modes")

    return _recorded("runbook_for", {"service": service}, body)


#: Facts behind a culprit's confidence. Read from the graph at write time, so the number stored
#: on `CAUSED_BY` is arithmetic over the graph rather than whatever the model estimated.
_CONFIDENCE_FACTS = """
MATCH (x:Alert {id: $aid})-[fo:FIRED_ON]->(f:Service)
WHERE ID(fo) >= 0
MATCH (c:Commit {sha: $sha})-[ci:IN]->(d:Deploy)
WHERE ID(ci) >= 0
OPTIONAL MATCH (d)-[t:TOUCHED]->(tch:Service)
WHERE ID(t) >= 0
WITH f, x, c, d,
     size([fl IN c.files WHERE ANY(owned IN f.paths WHERE fl STARTS WITH owned)]) AS overlap,
     size(c.files) AS files_changed,
     collect(DISTINCT tch.name) AS touched
RETURN f.name AS failing, x.at AS alert_at, d.at AS deploy_at, d.id AS deploy,
       overlap, files_changed, touched
"""

#: Dependency hops from the failing service to the nearest service the deploy touched.
#: `shortestPath` is only legal in a WITH or RETURN clause on FalkorDB — see docs/decisions.md.
_CONFIDENCE_DISTANCE = """
MATCH (f:Service {name: $failing})
UNWIND $touched AS tn
MATCH (t:Service {name: tn})
WITH shortestPath((f)-[:DEPENDS_ON*1..5]->(t)) AS p
WHERE p IS NOT NULL
RETURN min(length(p)) AS distance
"""


def confidence_facts(a: Ariadne, *, alert_id: str, sha: str) -> dict[str, Any] | None:
    """The terms behind a confidence score, read out of the graph.

    `distance` is 0 when the deploy touched the failing service itself, otherwise the fewest
    dependency hops from the failing service to a service that deploy touched. A deploy that
    touched neither the failing service nor anything it depends on cannot have reached it along
    the mesh, and scores at the maximum distance rather than being treated as adjacent.
    """
    row = a.one(_CONFIDENCE_FACTS, {"aid": alert_id, "sha": sha})
    if row is None:
        return None
    failing = row["failing"]
    touched = [t for t in (row["touched"] or []) if t]
    if failing in touched:
        distance = 0
    else:
        hop = a.one(_CONFIDENCE_DISTANCE, {"failing": failing, "touched": touched}) if touched \
            else None
        distance = (int(hop["distance"])
                    if hop is not None and hop["distance"] is not None else MAX_HOPS)
    return {
        "failing": failing,
        "alert_at": int(row["alert_at"]),
        "deploy": row["deploy"],
        "deploy_at": int(row["deploy_at"]),
        "overlap": int(row["overlap"]),
        "files_changed": int(row["files_changed"]),
        "distance": distance,
        "touched": touched,
    }


def computed_confidence(a: Ariadne, *, alert_id: str, sha: str) -> tuple[float, dict[str, Any]]:
    """`scoring.confidence` over those facts. Returns the score and the facts it came from."""
    facts = confidence_facts(a, alert_id=alert_id, sha=sha)
    if facts is None:
        raise KeyError(f"no alert {alert_id!r} or commit {sha!r} to score")
    return score_confidence(
        overlap=facts["overlap"], files_changed=facts["files_changed"],
        distance=facts["distance"], deploy_at=facts["deploy_at"],
        alert_at=facts["alert_at"],
    ), facts


# ======================================================================================
# write-back
# ======================================================================================

def record_outcome(
    incident_id: str,
    alert_id: str,
    signature: str,
    culprit_sha: str | None,
    confidence: float,
    impacted_services: list[str],
    resolved: bool = True,
) -> dict[str, Any]:
    """Write this investigation back to the graph as an incident and a reusable plan.

    Call this last, once you have a verdict. The *steps* are not an argument: they are taken from
    the tool calls actually made during this run, so a replayed plan reflects what happened rather
    than what the model remembers happening.

    Pass `culprit_sha: null` when no commit is responsible — a structural cause is a valid
    outcome and should be recorded as one.

    Args:
        incident_id: a new incident id, e.g. "inc-live-01".
        alert_id: the alert that triggered this.
        signature: the structural signature from the briefing.
        culprit_sha: the commit sha you concluded caused it, or null if none did.
        confidence: your own estimate, 0 to 1, that this commit is the cause. It is recorded
            as `model_confidence` for comparison and is *not* the number the verdict reports:
            `CAUSED_BY.confidence` is recomputed here from the graph (file overlap, dependency
            distance, deploy-to-alert minutes) so that "where did this number come from" has an
            arithmetic answer.
        impacted_services: service names from blast_radius.
        resolved: whether the investigation reached a verdict.
    """
    ctx = _ctx()
    calls = list(ctx.run.calls) if ctx.run else []

    def body(a: Ariadne) -> dict[str, Any]:
        at = int(time.time() * 1000)
        computed: float | None = None
        facts: dict[str, Any] | None = None

        # Scored before anything is written. A sha the graph does not have is a mistake worth
        # telling the model about, and failing here rather than between the incident write and
        # the plan write is what keeps a rejected call from leaving half an incident behind.
        if culprit_sha:
            try:
                computed, facts = computed_confidence(a, alert_id=alert_id, sha=culprit_sha)
            except KeyError:
                raise ToolError(
                    f"nothing written: no commit {culprit_sha!r} in a deploy reachable from "
                    f"alert {alert_id!r}. Pass a sha exactly as suspect_commits returned it, or "
                    f"culprit_sha: null if no commit is responsible."
                ) from None

        a.write("""
            MERGE (i:Incident {id: $iid})
              ON CREATE SET i.at = $at, i.status = $status, i.signature = $sig
            WITH i
            MATCH (x:Alert {id: $aid})
            MERGE (i)-[:TRIGGERED_BY]->(x)
        """, {"iid": incident_id, "at": at, "sig": signature, "aid": alert_id,
              "status": "resolved" if resolved else "open"})

        if culprit_sha and facts is not None:
            # The confidence computed above goes on the edge. The model's own estimate is kept
            # beside it rather than used: a number a model chose is not evidence, and the two
            # differing is itself worth being able to see.
            a.write("""
                MATCH (i:Incident {id: $iid}), (c:Commit {sha: $sha})
                MERGE (i)-[cb:CAUSED_BY]->(c)
                SET cb.confidence = $conf, cb.model_confidence = $model_conf,
                    cb.overlap = $overlap, cb.files_changed = $files_changed,
                    cb.distance_hops = $distance, cb.verified = false
            """, {"iid": incident_id, "sha": culprit_sha, "conf": float(computed),
                  "model_conf": float(confidence), "overlap": facts["overlap"],
                  "files_changed": facts["files_changed"], "distance": facts["distance"]})

        if impacted_services:
            a.write("""
                MATCH (i:Incident {id: $iid})
                UNWIND $names AS name
                MATCH (s:Service {name: name})
                MERGE (i)-[im:IMPACTED]->(s) ON CREATE SET im.depth = 1
            """, {"iid": incident_id, "names": list(impacted_services)})

        # The plan: one per signature, reinforced on every run.
        steps = [{
            "id": f"{incident_id}-s{c.order}", "order": c.order, "intent": c.intent(),
            "tool": c.tool, "kind": "write" if c.tool == "record_outcome" else "read",
            "args_shape": ",".join(sorted(c.args)), "result_id": f"{incident_id}-r{c.order}",
            "ok": c.ok, "rows": c.rows, "ms": c.ms,
            "tokens": 0,
        } for c in calls]

        run = ctx.run
        a.write("""
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
        """, {"sig": signature, "pid": f"plan-{signature}", "ok": bool(resolved),
              "tokens": int(run.tokens) if run else 0,
              "cost": float(run.cost) if run else 0.0,
              "iid": incident_id, "at": at})

        if steps:
            a.write("UNWIND $rows AS r MERGE (t:Tool {name: r.tool}) ON CREATE SET t.kind = r.kind",
                    {"rows": steps})
            a.write("""
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
            """, {"sig": signature, "rows": steps})

        out: dict[str, Any] = {"incident": incident_id, "plan_signature": signature,
                               "steps_recorded": len(steps), "culprit": culprit_sha}
        if computed is not None and facts is not None:
            # Handed back so the written verdict can quote the computed number rather than the
            # estimate the model arrived with.
            out["confidence"] = computed
            out["confidence_terms"] = {
                "path_overlap": f"{facts['overlap']}/{facts['files_changed']} changed files "
                                f"under {facts['failing']}'s owned paths",
                "distance_hops": facts["distance"],
                "deploy_to_alert_minutes": round(
                    (facts["alert_at"] - facts["deploy_at"]) / 60000, 1),
            }
            out["your_estimate_was"] = float(confidence)
            out["note"] = ("confidence is computed from the graph, not taken from your estimate; "
                           "report the `confidence` value above")
        return out

    return _recorded("record_outcome", {"incident_id": incident_id, "alert_id": alert_id}, body)


# ======================================================================================
# registry
# ======================================================================================

#: Plain callables, used by the tests and by the MCP server.
TOOL_FUNCS: dict[str, Callable[..., Any]] = {
    f.__name__: f for f in (
        recall_plan, prior_incidents, upstream_candidates, deploys_in_window,
        suspect_commits, blast_radius, owner_of, runbook_for, record_outcome,
    )
}

#: Read-only tools: safe to expose anywhere, and the only ones a replay needs.
READ_ONLY = tuple(n for n in TOOL_FUNCS if n != "record_outcome")

def _as_text(fn: Callable[..., Any]) -> Callable[..., str]:
    """Serialize a tool result for the Messages API.

    The SDK accepts a tool return of `None`, a `str`, or an *iterable of content blocks* — and a
    dict is iterable, so returning one makes the SDK iterate its **keys** and the API rejects
    them with `tool_result.content.0: Input should be an object`. Returning JSON text is the
    correct shape, and it only applies to this transport: MCP and the tests keep the structured
    dicts, which is why the serialization lives at the boundary rather than inside each tool.
    """
    @functools.wraps(fn)
    def call(*args: Any, **kwargs: Any) -> str:
        return json.dumps(fn(*args, **kwargs), default=str)

    call.__annotations__ = {**getattr(fn, "__annotations__", {}), "return": str}
    return call


#: The same functions wrapped for the Anthropic tool runner. The last one carries the cache
#: breakpoint: tool definitions are the most stable part of the prompt, so everything up to here
#: is cached and only the incident briefing varies per request.
TOOLS = [beta_tool(_as_text(f)) for f in TOOL_FUNCS.values()]
TOOLS[-1] = beta_tool(_as_text(TOOL_FUNCS["record_outcome"]),
                      cache_control={"type": "ephemeral"})
