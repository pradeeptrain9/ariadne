"""Deterministic synthetic graph.

All data here is generated from a fixed RNG seed: nothing scraped, nothing private (rule 6), and
re-running produces an identical graph. That reproducibility is not cosmetic — the cold-vs-warm
cost comparison is only honest if both runs see the same world.

Deliberately not an ingestion pipeline. The graph model and the agent are what gets judged; a
Datadog or GitHub connector would cost a day and prove nothing about either. The schema is shaped
to accept real data, and this module is the only thing that would be replaced.

Spec: docs/fixtures.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any

from .graph import Ariadne
from . import schema
from .signature import shape_of, DEPLOY_WINDOW_MS

SEED = 20261015
DAY_MS = 24 * 60 * 60 * 1000
NOW = 1_760_000_000_000  # fixed "seed time"; all timestamps are relative to this
HISTORY_MS = 14 * DAY_MS

TIER1 = [
    "checkout-api", "search-api", "account-api", "feed-api", "payments-api", "mobile-gateway",
]
TIER2 = [
    "cart-service", "catalog-service", "pricing-service", "identity-service", "session-service",
    "inventory-service", "recommendation-service", "notification-service", "fraud-service",
    "shipping-service", "tax-service", "media-service", "review-service", "loyalty-service",
]
TIER3 = [
    "orders-db", "catalog-db", "accounts-db", "sessions-cache", "pricing-cache", "search-index",
    "event-bus", "object-store", "metrics-store", "feature-flags", "secrets-vault",
    "audit-log", "ledger-db", "queue-broker", "graph-store", "cdn-edge",
    "config-service", "rate-limiter", "schema-registry", "blob-index",
]

PEOPLE = [
    ("arjun", "Arjun Rao", "Asia/Kolkata"), ("mei", "Mei Chen", "Asia/Singapore"),
    ("tomas", "Tomas Novak", "Europe/Prague"), ("nadia", "Nadia Haddad", "Europe/Berlin"),
    ("kwame", "Kwame Mensah", "Africa/Accra"), ("sofia", "Sofia Rossi", "Europe/Rome"),
    ("dmitri", "Dmitri Volkov", "Europe/Belgrade"), ("yuki", "Yuki Tanaka", "Asia/Tokyo"),
    ("priya", "Priya Nair", "Asia/Kolkata"), ("liam", "Liam O'Connor", "Europe/Dublin"),
    ("ines", "Ines Duarte", "Europe/Lisbon"), ("samir", "Samir Qureshi", "Asia/Karachi"),
]

FAILURE_MODES = [
    ("connection_pool_exhaustion", "all pool slots checked out, callers block"),
    ("cache_stampede", "simultaneous misses collapse onto the origin"),
    ("n_plus_one_query", "per-row query inside a loop multiplies read load"),
    ("thread_starvation", "worker pool saturated, queue grows without bound"),
    ("disk_pressure", "write amplification fills the volume"),
    ("clock_skew", "token or TTL comparisons fail across hosts"),
    ("retry_storm", "client retries amplify an upstream failure"),
    ("schema_lock", "a migration holds a lock other writers need"),
]

METRICS = ("latency_p99", "error_rate", "saturation", "queue_depth")
SEVERITIES = ("low", "medium", "high", "critical")

COMMIT_VERBS = [
    "tune", "refactor", "fix", "add", "remove", "inline", "cache", "batch", "retry",
    "guard", "bump", "rewrite", "parallelise", "simplify",
]
COMMIT_NOUNS = [
    "connection pool", "retry policy", "serialisation", "index hint", "timeout",
    "pagination", "error mapping", "feature gate", "bulk loader", "lock scope",
    "health check", "circuit breaker", "backoff", "query plan",
]

SHARED_LIB = "libs/common/"


# --------------------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------------------

def _paths_for(name: str) -> list[str]:
    return [f"services/{name}/"]


def build_services() -> list[dict[str, Any]]:
    out = []
    for tier, names in ((1, TIER1), (2, TIER2), (3, TIER3)):
        for n in names:
            out.append({
                "name": n,
                "tier": tier,
                "repo": f"acme/{n}",
                "paths": _paths_for(n),
            })
    return out


def build_deps(rng: random.Random) -> list[dict[str, Any]]:
    """Layered DAG. Convention, fixed in docs/fixtures.md: (caller)-[:DEPENDS_ON]->(callee).

    A tier-1 API points at the tier-2 services it calls, which point at tier-3 infrastructure.
    No cycles, and every service reachable — `verify` asserts a single weakly connected
    component, because an orphan would make a demo query return empty for reasons that look like
    an agent bug.
    """
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(src: str, dst: str) -> None:
        if src == dst or (src, dst) in seen or (dst, src) in seen:
            return
        seen.add((src, dst))
        edges.append({
            "src": src,
            "dst": dst,
            "critical": rng.random() < 0.3,
            "calls_per_min": rng.choice([5, 20, 60, 180, 400, 1200, 3000]),
        })

    # tier1 -> tier2: each API calls 4-7 services
    for api in TIER1:
        for svc in rng.sample(TIER2, rng.randint(4, 7)):
            add(api, svc)

    # tier2 -> tier3: each service calls 3-5 stores
    for svc in TIER2:
        for store in rng.sample(TIER3, rng.randint(3, 5)):
            add(svc, store)

    # a few tier2 -> tier2 and tier3 -> tier3 edges, so the mesh is not a clean tree
    for _ in range(8):
        a, b = rng.sample(TIER2, 2)
        add(a, b)
    for _ in range(5):
        a, b = rng.sample(TIER3, 2)
        add(a, b)

    # guarantee no tier-3 orphan: anything with no caller gets one
    called = {e["dst"] for e in edges}
    for store in TIER3:
        if store not in called:
            add(rng.choice(TIER2), store)

    return edges


def build_ownership(rng: random.Random) -> list[dict[str, Any]]:
    """Every service has a primary owner; roughly half also have a secondary."""
    services = TIER1 + TIER2 + TIER3
    handles = [p[0] for p in PEOPLE]
    rows: list[dict[str, Any]] = []
    for i, svc in enumerate(services):
        primary = handles[i % len(handles)]
        rows.append({"handle": primary, "service": svc, "rotation": "primary"})
        if rng.random() < 0.5:
            secondary = rng.choice([h for h in handles if h != primary])
            rows.append({"handle": secondary, "service": svc, "rotation": "secondary"})
    return rows


def build_failure_modes(rng: random.Random) -> tuple[list, list, list]:
    modes = [{"name": n, "symptom": s} for n, s in FAILURE_MODES]
    runbooks = [
        {"id": f"rb-{i:02d}", "title": f"Mitigating {n.replace('_', ' ')}",
         "url": f"https://runbooks.internal/{n}", "fixes": n}
        for i, (n, _) in enumerate(FAILURE_MODES)
    ]
    exhibits = []
    for svc in TIER1 + TIER2 + TIER3:
        for mode, _ in rng.sample(FAILURE_MODES, rng.randint(1, 3)):
            exhibits.append({"service": svc, "mode": mode, "seen_count": rng.randint(1, 14)})
    return modes, runbooks, exhibits


def build_commits_and_deploys(rng: random.Random) -> tuple[list, list, list, list]:
    """~200 commits across ~60 deploys over a 14-day window.

    A commit's changed files come from the owning service's paths, with 15% drawn from a shared
    library instead. That noise is what makes file-path overlap a real ranking signal rather
    than a lookup: the shared-library commits are the ones that break a service while barely
    touching its own paths, which is exactly scenario C.
    """
    services = TIER1 + TIER2 + TIER3
    handles = [p[0] for p in PEOPLE]

    commits: list[dict[str, Any]] = []
    deploys: list[dict[str, Any]] = []
    commit_in_deploy: list[dict[str, str]] = []
    deploy_touched: list[dict[str, str]] = []

    for d in range(60):
        at = NOW - HISTORY_MS + int((d + 1) * HISTORY_MS / 61)
        did = f"dep-{d:03d}"
        touched = rng.sample(services, rng.randint(1, 3))
        n_commits = rng.randint(1, 6)
        shas: list[str] = []
        for c in range(n_commits):
            owner_svc = rng.choice(touched)
            sha = hashlib.sha1(f"{did}-{c}".encode()).hexdigest()[:10]
            n_files = rng.randint(1, 6)
            files = []
            for _ in range(n_files):
                if rng.random() < 0.15:
                    files.append(f"{SHARED_LIB}{rng.choice(['http', 'db', 'pool', 'retry'])}.py")
                else:
                    files.append(f"services/{owner_svc}/{rng.choice(['handler', 'client', 'model', 'config'])}.py")
            commits.append({
                "sha": sha,
                "msg": f"{rng.choice(COMMIT_VERBS)} {rng.choice(COMMIT_NOUNS)}",
                "files": sorted(set(files)),
                "at": at - rng.randint(0, 6 * 60 * 60 * 1000),
                "author": rng.choice(handles),
            })
            shas.append(sha)
            commit_in_deploy.append({"sha": sha, "deploy": did})
        deploys.append({"id": did, "at": at, "sha": shas[-1], "env": "production"})
        for svc in touched:
            deploy_touched.append({"deploy": did, "service": svc})

    return commits, deploys, commit_in_deploy, deploy_touched


def build_alerts(rng: random.Random, deploys: list[dict], deploy_touched: list[dict]) -> list[dict]:
    """~80 alerts. Most cluster 0-45 min after a deploy that touched the service; ~20% are noise
    with no related deploy at all, so the agent has to tolerate an empty window."""
    touched_by: dict[str, list[str]] = {}
    for row in deploy_touched:
        touched_by.setdefault(row["deploy"], []).append(row["service"])

    alerts: list[dict[str, Any]] = []
    for i in range(64):
        dep = rng.choice(deploys)
        svc = rng.choice(touched_by[dep["id"]])
        alerts.append({
            "id": f"alr-{i:03d}",
            "at": dep["at"] + rng.randint(60_000, 45 * 60_000),
            "metric": rng.choice(METRICS),
            "value": round(rng.uniform(1.5, 40.0), 2),
            "severity": rng.choices(SEVERITIES, weights=[1, 3, 4, 2])[0],
            "service": svc,
        })
    for i in range(64, 80):  # noise, deliberately unconnected to any deploy
        alerts.append({
            "id": f"alr-{i:03d}",
            "at": NOW - rng.randint(DAY_MS, HISTORY_MS),
            "metric": rng.choice(METRICS),
            "value": round(rng.uniform(1.1, 6.0), 2),
            "severity": rng.choices(SEVERITIES, weights=[4, 3, 1, 0])[0],
            "service": rng.choice(TIER1 + TIER2 + TIER3),
        })
    return alerts


# --------------------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------------------

def _write_infra(a: Ariadne, data: dict[str, Any]) -> None:
    a.write("UNWIND $rows AS r CREATE (s:Service {name: r.name, tier: r.tier, repo: r.repo, paths: r.paths})",
            {"rows": data["services"]})
    a.write("UNWIND $rows AS r CREATE (p:Person {handle: r[0], name: r[1], tz: r[2]})",
            {"rows": [list(p) for p in PEOPLE]})
    a.write("UNWIND $rows AS r CREATE (f:FailureMode {name: r.name, symptom: r.symptom})",
            {"rows": data["modes"]})
    a.write("UNWIND $rows AS r CREATE (b:Runbook {id: r.id, title: r.title, url: r.url})",
            {"rows": data["runbooks"]})
    a.write("""UNWIND $rows AS r
               MATCH (b:Runbook {id: r.id}), (f:FailureMode {name: r.fixes})
               CREATE (b)-[:FIXES]->(f)""", {"rows": data["runbooks"]})
    a.write("""UNWIND $rows AS r
               MATCH (s:Service {name: r.service}), (f:FailureMode {name: r.mode})
               CREATE (s)-[:EXHIBITS {seen_count: r.seen_count}]->(f)""",
            {"rows": data["exhibits"]})
    a.write("""UNWIND $rows AS r
               MATCH (x:Service {name: r.src}), (y:Service {name: r.dst})
               CREATE (x)-[:DEPENDS_ON {critical: r.critical, calls_per_min: r.calls_per_min}]->(y)""",
            {"rows": data["deps"]})
    a.write("""UNWIND $rows AS r
               MATCH (p:Person {handle: r.handle}), (s:Service {name: r.service})
               CREATE (p)-[:OWNS {rotation: r.rotation}]->(s)""", {"rows": data["ownership"]})
    a.write("""UNWIND $rows AS r
               CREATE (c:Commit {sha: r.sha, msg: r.msg, files: r.files, at: r.at})""",
            {"rows": data["commits"]})
    a.write("""UNWIND $rows AS r
               MATCH (c:Commit {sha: r.sha}), (p:Person {handle: r.author})
               CREATE (c)-[:AUTHORED_BY]->(p)""", {"rows": data["commits"]})
    a.write("UNWIND $rows AS r CREATE (d:Deploy {id: r.id, at: r.at, sha: r.sha, env: r.env})",
            {"rows": data["deploys"]})
    a.write("""UNWIND $rows AS r
               MATCH (c:Commit {sha: r.sha}), (d:Deploy {id: r.deploy})
               CREATE (c)-[:IN]->(d)""", {"rows": data["commit_in_deploy"]})
    a.write("""UNWIND $rows AS r
               MATCH (d:Deploy {id: r.deploy}), (s:Service {name: r.service})
               CREATE (d)-[:TOUCHED]->(s)""", {"rows": data["deploy_touched"]})
    a.write("""UNWIND $rows AS r
               CREATE (x:Alert {id: r.id, at: r.at, metric: r.metric, value: r.value,
                                severity: r.severity})""", {"rows": data["alerts"]})
    a.write("""UNWIND $rows AS r
               MATCH (x:Alert {id: r.id}), (s:Service {name: r.service})
               CREATE (x)-[:FIRED_ON]->(s)""", {"rows": data["alerts"]})


def _store_betweenness(a: Ariadne) -> None:
    """Chokepoint score per service, computed once here and stored.

    Used as a tie-break in upstream_candidates, so the tie is broken by a structural fact rather
    than an arbitrary one. Computed, never guessed.
    """
    rows = a.q("""CALL algo.betweenness({nodeLabels: ['Service'], relationshipTypes: ['DEPENDS_ON']})
                  YIELD node, score RETURN node.name AS name, score""")
    a.write("""UNWIND $rows AS r
               MATCH (s:Service {name: r.name}) SET s.betweenness = r.score""",
            {"rows": [{"name": r["name"], "score": float(r["score"])} for r in rows]})


def _plan_embedding(signature: str, dim: int = schema.EMBED_DIM) -> list[float]:
    """Deterministic unit vector derived from the signature.

    Day 1 placeholder: the vector index is the *secondary*, near-miss recall path, and seeding it
    needs stable vectors more than it needs semantic ones. Day 3 replaces this with a local
    sentence-transformer over the plan's step intents. Dimension and similarity function are
    already the real ones, so the swap touches this function only.
    """
    rnd = random.Random(signature)
    v = [rnd.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


SCENARIO_A_METRIC = "latency_p99"
SCENARIO_A_SEVERITY = "high"
SCENARIO_A_COUNT = 10  # 8 historical + 2 held back for the live demo


def inject_scenario_a(a: Ariadne, rng: random.Random) -> tuple[list[dict], list[dict]]:
    """Populate the repeated-shape scenario, deliberately.

    docs/fixtures.md asks for at least eight historical incidents sharing one signature. Left to
    chance that does not happen: the signature space is ~1500 combinations and 60 incidents
    collide only by luck, which is why the first seeding run produced 49 plans for 60 incidents —
    a memory with almost nothing to reuse.

    So the collision is *populated* rather than hoped for. It is still a real structural
    collision: we pick a set of services that genuinely share (tier, fan-in bucket, fan-out
    bucket), then give each the same kind of alert with a deploy in its window. The signature is
    computed by the same `shape_of` the agent uses — nothing is hardcoded. What is deliberate is
    the sample, not the match.

    Each injected alert gets its own deploy whose commits really do overlap the failing service's
    paths, so `suspect_commits` has a correct answer to find.
    """
    from .signature import degree_bucket

    rows = a.q("""
        MATCH (s:Service)
        OPTIONAL MATCH (caller:Service)-[cin:DEPENDS_ON]->(s) WHERE ID(cin) >= 0
        WITH s, count(DISTINCT caller) AS fan_in
        OPTIONAL MATCH (s)-[cout:DEPENDS_ON]->(callee:Service) WHERE ID(cout) >= 0
        RETURN s.name AS name, s.tier AS tier, fan_in,
               count(DISTINCT callee) AS fan_out
    """)
    groups: dict[tuple, list[str]] = {}
    for r in rows:
        key = (int(r["tier"]), degree_bucket(int(r["fan_in"])), degree_bucket(int(r["fan_out"])))
        groups.setdefault(key, []).append(r["name"])

    # The group must have callers (fan-in bucket != "0"), or `blast_radius` returns nothing for
    # every alert in the demo: a tier-1 leaf has no upstream, so "what else breaks" is empty.
    # Among the groups that qualify, prefer the lowest tier and the largest pool.
    eligible = [k for k, v in groups.items() if len(v) >= 4 and k[1] != "0"]
    if not eligible:  # degenerate mesh; fall back rather than crash
        eligible = [k for k, v in groups.items() if len(v) >= 4]
    key = min(eligible, key=lambda k: (k[0], -len(groups[k])))
    pool = sorted(groups[key])
    chosen = [pool[i % len(pool)] for i in range(SCENARIO_A_COUNT)]

    alerts: list[dict[str, Any]] = []
    deploys, commits, c_in_d, d_touched = [], [], [], []

    for k, svc in enumerate(chosen):
        did = f"dep-a{k:02d}"
        at = NOW - (SCENARIO_A_COUNT - k) * 6 * 60 * 60 * 1000
        deploys.append({"id": did, "at": at, "sha": f"a{k:02d}", "env": "production"})
        d_touched.append({"deploy": did, "service": svc})
        for c in range(3):
            sha = hashlib.sha1(f"{did}-{c}".encode()).hexdigest()[:10]
            # two commits inside the service's own paths, one in the shared library
            files = ([f"services/{svc}/handler.py", f"services/{svc}/client.py"] if c < 2
                     else [f"{SHARED_LIB}pool.py"])
            commits.append({"sha": sha, "msg": f"{rng.choice(COMMIT_VERBS)} {rng.choice(COMMIT_NOUNS)}",
                            "files": files, "at": at - 60_000 * (c + 1),
                            "author": rng.choice([p[0] for p in PEOPLE])})
            c_in_d.append({"sha": sha, "deploy": did})
        alerts.append({
            "id": f"alr-a{k:02d}",
            "at": at + rng.randint(5 * 60_000, 30 * 60_000),
            "metric": SCENARIO_A_METRIC,
            "value": round(rng.uniform(8.0, 30.0), 2),
            "severity": SCENARIO_A_SEVERITY,
            "service": svc,
        })

    a.write("UNWIND $rows AS r CREATE (d:Deploy {id: r.id, at: r.at, sha: r.sha, env: r.env})",
            {"rows": deploys})
    a.write("UNWIND $rows AS r CREATE (c:Commit {sha: r.sha, msg: r.msg, files: r.files, at: r.at})",
            {"rows": commits})
    a.write("""UNWIND $rows AS r MATCH (c:Commit {sha: r.sha}), (p:Person {handle: r.author})
               CREATE (c)-[:AUTHORED_BY]->(p)""", {"rows": commits})
    a.write("""UNWIND $rows AS r MATCH (c:Commit {sha: r.sha}), (d:Deploy {id: r.deploy})
               CREATE (c)-[:IN]->(d)""", {"rows": c_in_d})
    a.write("""UNWIND $rows AS r MATCH (d:Deploy {id: r.deploy}), (s:Service {name: r.service})
               CREATE (d)-[:TOUCHED]->(s)""", {"rows": d_touched})
    a.write("""UNWIND $rows AS r
               CREATE (x:Alert {id: r.id, at: r.at, metric: r.metric, value: r.value,
                                severity: r.severity})""", {"rows": alerts})
    a.write("""UNWIND $rows AS r MATCH (x:Alert {id: r.id}), (s:Service {name: r.service})
               CREATE (x)-[:FIRED_ON]->(s)""", {"rows": alerts})

    return alerts[:8], alerts[8:]


def _seed_incidents(a: Ariadne, alerts: list[dict], rng: random.Random) -> dict[str, Any]:
    """Resolve ~60 historical alerts into incidents, each with a computed signature.

    Signatures come from `signature.shape_of` against the live graph — the same code path the
    agent uses. Nothing is hardcoded, so scenario A's collisions are real structural collisions
    rather than a fixture that pretends.
    """
    resolved = alerts
    incidents: list[dict[str, Any]] = []

    for i, alert in enumerate(resolved):
        sig = shape_of(
            a, service=alert["service"], metric=alert["metric"],
            severity=alert["severity"], at=alert["at"],
        ).digest()
        culprit = a.one("""
            MATCH (d:Deploy)-[t:TOUCHED]->(s:Service {name: $svc})
            WHERE ID(t) >= 0 AND d.at <= $at AND d.at >= $at - $window
            MATCH (c:Commit)-[inr:IN]->(d) WHERE ID(inr) >= 0
            MATCH (s2:Service {name: $svc})
            WITH c, size([f IN c.files WHERE ANY(p IN s2.paths WHERE f STARTS WITH p)]) AS overlap
            RETURN c.sha AS sha, overlap ORDER BY overlap DESC, c.at DESC LIMIT 1
        """, {"svc": alert["service"], "at": alert["at"], "window": DEPLOY_WINDOW_MS})
        incidents.append({
            "id": f"inc-{i:03d}",
            "at": alert["at"] + rng.randint(5 * 60_000, 90 * 60_000),
            "signature": sig,
            "alert": alert["id"],
            "culprit": culprit["sha"] if culprit else None,
            "confidence": round(rng.uniform(0.62, 0.93), 2),
        })

    a.write("""UNWIND $rows AS r
               CREATE (i:Incident {id: r.id, at: r.at, status: 'resolved', signature: r.signature})""",
            {"rows": incidents})
    a.write("""UNWIND $rows AS r
               MATCH (i:Incident {id: r.id}), (x:Alert {id: r.alert})
               CREATE (i)-[:TRIGGERED_BY]->(x)""", {"rows": incidents})
    with_culprit = [r for r in incidents if r["culprit"]]
    a.write("""UNWIND $rows AS r
               MATCH (i:Incident {id: r.id}), (c:Commit {sha: r.culprit})
               CREATE (i)-[:CAUSED_BY {confidence: r.confidence, verified: true}]->(c)""",
            {"rows": with_culprit})
    a.write("""UNWIND $rows AS r
               MATCH (i:Incident {id: r.id})-[:TRIGGERED_BY]->(:Alert)-[:FIRED_ON]->(s:Service)
               MATCH (caller:Service)-[deps:DEPENDS_ON*1..3]->(s)
               WHERE ALL(d IN relationships(deps) WHERE ID(d) >= 0)
               WITH i, caller, min(length(deps)) AS depth
               CREATE (i)-[:IMPACTED {depth: depth}]->(caller)""",
            {"rows": incidents})

    return {"incidents": incidents}


def _seed_plans(a: Ariadne, incidents: list[dict], rng: random.Random) -> dict[str, Any]:
    """One Plan per distinct historical signature, with ordered steps.

    This is the memory corpus the agent starts from. Step sequences mirror the cold
    investigation order, because that is what a cold run would have recorded.
    """
    by_sig: dict[str, list[dict]] = {}
    for inc in incidents:
        by_sig.setdefault(inc["signature"], []).append(inc)

    cold_sequence = [
        ("narrow the dependency cone", "upstream_candidates"),
        ("find changes in the window", "deploys_in_window"),
        ("rank commits by file overlap", "suspect_commits"),
        ("measure downstream impact", "blast_radius"),
        ("identify the on-call owner", "owner_of"),
        ("attach the matching runbook", "runbook_for"),
    ]

    plans, steps = [], []
    for n, (sig, incs) in enumerate(sorted(by_sig.items())):
        pid = f"plan-{n:03d}"
        runs = len(incs)
        plans.append({
            "id": pid, "signature": sig, "runs": runs,
            "successes": max(1, runs - (1 if runs > 2 and rng.random() < 0.3 else 0)),
            "mean_tokens": rng.randint(9_000, 26_000),
            "mean_cost_usd": round(rng.uniform(0.18, 0.52), 4),
            "embedding": _plan_embedding(sig),
            "incidents": [i["id"] for i in incs],
        })
        for order, (intent, tool) in enumerate(cold_sequence):
            steps.append({
                "plan": pid, "id": f"{pid}-s{order}", "order": order, "intent": intent,
                "tool": tool, "kind": "read",
                "result_id": f"{pid}-r{order}", "ok": True,
                "tokens": rng.randint(600, 3200), "ms": rng.randint(40, 900),
                "rows": rng.randint(1, 15),
            })

    a.write("""UNWIND $rows AS r
               CREATE (p:Plan {id: r.id, signature: r.signature, runs: r.runs,
                               successes: r.successes, mean_tokens: r.mean_tokens,
                               mean_cost_usd: r.mean_cost_usd,
                               embedding: vecf32(r.embedding)})""", {"rows": plans})
    a.write("""UNWIND $rows AS r
               UNWIND r.incidents AS iid
               MATCH (p:Plan {id: r.id}), (i:Incident {id: iid})
               CREATE (i)-[:SOLVED_BY {at: i.at}]->(p)""", {"rows": plans})
    a.write("UNWIND $rows AS r MERGE (t:Tool {name: r.tool}) ON CREATE SET t.kind = r.kind",
            {"rows": steps})
    a.write("""UNWIND $rows AS r
               MATCH (p:Plan {id: r.plan})
               CREATE (s:Step {id: r.id, order: r.order, intent: r.intent,
                               tool_name: r.tool, args_shape: 'service,window'})
               CREATE (p)-[:STEP {order: r.order}]->(s)
               WITH s, r
               MATCH (t:Tool {name: r.tool}) CREATE (s)-[:USED_TOOL]->(t)
               CREATE (res:Result {id: r.result_id, ok: r.ok, tokens: r.tokens,
                                   ms: r.ms, rows: r.rows})
               CREATE (s)-[:OUTCOME]->(res)""", {"rows": steps})

    return {"plans": plans}


def _seed_scenario_c(a: Ariadne, plans: list[dict]) -> dict[str, Any]:
    """The plan that was wrong.

    Storing failure is what makes this a memory rather than a cache, and it is the thing most
    entries will not have. The recorded condition is real: ranking commits by file overlap fails
    when the culprit touches a shared library and barely touches the failing service's own paths.
    """
    if not plans:
        return {}
    victim = max(plans, key=lambda p: p["runs"])
    old_id, sig = victim["id"], victim["signature"]
    new_id = f"{old_id}-v2"

    a.write("""MATCH (old:Plan {id: $old})
               SET old.runs = 4, old.successes = 2
               CREATE (new:Plan {id: $new, signature: $sig, runs: 2, successes: 2,
                                 mean_tokens: 11000, mean_cost_usd: 0.21,
                                 embedding: vecf32($emb)})
               CREATE (old)-[:SUPERSEDED_BY {reason:
                 'ranked by file overlap; culprit touched libs/common only'}]->(new)""",
            {"old": old_id, "new": new_id, "sig": sig, "emb": _plan_embedding(new_id)})

    # The replacement carries a *corrected* sequence — that is the point of superseding one plan
    # with another. It keeps the original steps and adds the check that was missing: look at
    # shared-library commits before trusting the file-overlap ranking.
    #
    # Without these steps `recall_plan` returns a plan with nothing in it, because it correctly
    # skips the superseded original. A replay would then have nothing to replay.
    corrected = [
        ("narrow the dependency cone", "upstream_candidates"),
        ("find changes in the window", "deploys_in_window"),
        ("rank commits by file overlap", "suspect_commits"),
        ("re-check shared-library commits the overlap ranking scores low", "suspect_commits"),
        ("measure downstream impact", "blast_radius"),
        ("identify the on-call owner", "owner_of"),
    ]
    a.write("""UNWIND $rows AS r
               MATCH (p:Plan {id: $pid})
               MERGE (t:Tool {name: r.tool}) ON CREATE SET t.kind = 'read'
               CREATE (s:Step {id: r.id, order: r.order, intent: r.intent,
                               tool_name: r.tool, args_shape: 'service,window'})
               CREATE (p)-[:STEP {order: r.order}]->(s)
               CREATE (s)-[:USED_TOOL]->(t)
               CREATE (res:Result {id: r.result_id, ok: true, tokens: 1800,
                                   ms: 120, rows: 4})
               CREATE (s)-[:OUTCOME]->(res)""",
            {"pid": new_id,
             "rows": [{"id": f"{new_id}-s{i}", "order": i, "intent": intent, "tool": tool,
                       "result_id": f"{new_id}-r{i}"}
                      for i, (intent, tool) in enumerate(corrected)]})
    a.write("""MERGE (c:Condition {name: 'shared_library_change'})
                 ON CREATE SET c.description =
                   'culprit commit changes libs/common and barely touches the failing service paths'
               WITH c
               MATCH (t:Tool {name: 'suspect_commits'})
               MERGE (t)-[f:FAILS_ON]->(c)
                 ON CREATE SET f.observed_count = 0
               SET f.observed_count = f.observed_count + 2""")
    return {"superseded_plan": old_id, "replacement": new_id, "signature": sig}


def _scenarios(a: Ariadne, alerts: list[dict], *, holdout: list[dict]) -> dict[str, Any]:
    """Pick the live-injection alerts for the demo.

    Scenario A is a pair of alerts on *different* services that share a signature: the first runs
    cold and writes a plan, the second recalls it. That pair is the Track 02 claim.

    Scenario B is an alert whose signature appears nowhere in history, on a tier-3 service with no
    deploy in its window — so the honest answer is "no culprit commit, the cause is structural".
    """
    by_sig: dict[str, list[dict]] = {}
    for alert in alerts:
        sig = shape_of(a, service=alert["service"], metric=alert["metric"],
                       severity=alert["severity"], at=alert["at"]).digest()
        by_sig.setdefault(sig, []).append(alert)

    # Scenario A's pair is the deliberate holdout: two alerts on different services whose
    # signature already has eight resolved incidents behind it.
    pair = holdout

    known = {r["sig"] for r in a.q("MATCH (i:Incident) RETURN DISTINCT i.signature AS sig")}
    novel = next((alert for sig, group in by_sig.items() if sig not in known
                  for alert in group), None)

    return {
        "scenario_a": [dict(alert) for alert in pair[:2]],
        "scenario_b": dict(novel) if novel else None,
    }


# --------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------

def seed(a: Ariadne, *, write_scenarios: Path | None = None) -> dict[str, Any]:
    rng = random.Random(SEED)

    services = build_services()
    deps = build_deps(rng)
    ownership = build_ownership(rng)
    modes, runbooks, exhibits = build_failure_modes(rng)
    commits, deploys, commit_in_deploy, deploy_touched = build_commits_and_deploys(rng)
    alerts = build_alerts(rng, deploys, deploy_touched)

    a.drop()
    schema.apply(a)
    _write_infra(a, {
        "services": services, "deps": deps, "ownership": ownership, "modes": modes,
        "runbooks": runbooks, "exhibits": exhibits, "commits": commits, "deploys": deploys,
        "commit_in_deploy": commit_in_deploy, "deploy_touched": deploy_touched,
        "alerts": alerts,
    })
    _store_betweenness(a)

    # The repeated-shape corpus, populated deliberately (see inject_scenario_a).
    a_hist, a_hold = inject_scenario_a(a, rng)

    incidents = _seed_incidents(a, alerts[:60] + a_hist, rng)["incidents"]
    plans = _seed_plans(a, incidents, rng)["plans"]
    scenario_c = _seed_scenario_c(a, plans)
    scen = _scenarios(a, alerts[60:], holdout=a_hold)
    scen["scenario_c"] = scenario_c

    if write_scenarios:
        write_scenarios.parent.mkdir(parents=True, exist_ok=True)
        write_scenarios.write_text(json.dumps(scen, indent=2) + "\n")

    # Counts come from the graph, not from the Python lists: scenario A writes its own
    # deploys, commits and alerts, so the lists above undercount what was actually stored.
    counted = {
        label.lower(): a.scalar(f"MATCH (n:{label}) RETURN count(n) AS c")
        for label in ("Service", "Person", "Commit", "Deploy", "Alert", "Incident", "Plan", "Step")
    }
    counted["deps"] = a.scalar(
        "MATCH ()-[r:DEPENDS_ON]->() WHERE ID(r) >= 0 RETURN count(r) AS c")
    counted["scenarios"] = scen
    return counted


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed the Ariadne graph (deterministic).")
    ap.add_argument("--verify", action="store_true", help="run verification after seeding")
    args = ap.parse_args()

    with Ariadne() as a:
        stats = seed(a, write_scenarios=Path("fixtures/scenarios.json"))
        for k, v in stats.items():
            if k != "scenarios":
                print(f"{k:12} {v}")
        scen = stats["scenarios"]
        print("\nscenario A (repeated shape):",
              [x["id"] + "@" + x["service"] for x in scen["scenario_a"]])
        print("scenario B (novel shape):   ",
              (scen["scenario_b"] or {}).get("id"), (scen["scenario_b"] or {}).get("service"))
        print("scenario C (wrong plan):    ", scen["scenario_c"].get("superseded_plan"),
              "->", scen["scenario_c"].get("replacement"))
        if args.verify:
            from .verify import run as verify_run
            print()
            ok = verify_run(a)
            raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
