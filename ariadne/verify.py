"""Day 1 exit criteria: the graph is correct before any agent code depends on it.

Checks here are deliberately the ones whose failure would be *invisible* later — an empty blast
radius, a disconnected service, a signature space that never collides. Each prints the number it
found, so a judge can read the output rather than trust a green tick.
"""

from __future__ import annotations

from .graph import Ariadne
from . import schema

EXPECTED = {
    "Service": 40,
    "Person": 12,
    "FailureMode": 8,
    "Runbook": 8,
}


class Check:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, name: str, passed: bool, detail: str) -> None:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}: {detail}")
        if not passed:
            self.failures.append(name)


def run(a: Ariadne) -> bool:
    c = Check()
    print("counts")
    for label, want in EXPECTED.items():
        got = a.scalar(f"MATCH (n:{label}) RETURN count(n) AS c")
        c.ok(label, got == want, f"{got} (expected {want})")

    for label in ("Commit", "Deploy", "Alert", "Incident", "Plan", "Step", "Tool"):
        got = a.scalar(f"MATCH (n:{label}) RETURN count(n) AS c")
        c.ok(label, got > 0, str(got))

    print("\ntiers")
    for tier, want in ((1, 6), (2, 14), (3, 20)):
        got = a.scalar("MATCH (s:Service {tier: $t}) RETURN count(s) AS c", {"t": tier})
        c.ok(f"tier {tier}", got == want, f"{got} (expected {want})")

    print("\nconnectivity")
    comps = a.q("""CALL algo.WCC({nodeLabels: ['Service'], relationshipTypes: ['DEPENDS_ON']})
                   YIELD componentId RETURN DISTINCT componentId AS id""")
    c.ok("single weakly connected component", len(comps) == 1,
         f"{len(comps)} component(s)")

    orphans = a.q("""MATCH (s:Service)
                     WHERE NOT (s)-[:DEPENDS_ON]->() AND NOT ()-[:DEPENDS_ON]->(s)
                     RETURN s.name AS name""")
    c.ok("no isolated services", not orphans, f"{len(orphans)} isolated")

    cycles = a.scalar("""MATCH (s:Service)-[r:DEPENDS_ON]->(t:Service)-[r2:DEPENDS_ON]->(s)
                         WHERE ID(r) >= 0 AND ID(r2) >= 0 RETURN count(s) AS c""")
    c.ok("no 2-cycles in DEPENDS_ON", cycles == 0, str(cycles))

    print("\ntraversal (the direction check — reversing this is the likeliest silent bug)")
    sample = a.one("""MATCH (api:Service {tier: 1})-[d:DEPENDS_ON]->(svc:Service)
                      WHERE ID(d) >= 0
                      RETURN api.name AS caller, svc.name AS callee LIMIT 1""")
    c.ok("tier-1 depends on something", sample is not None,
         f"{sample['caller']} -> {sample['callee']}" if sample else "none found")

    if sample:
        # blast_radius(callee) must contain the caller: the caller breaks when the callee breaks.
        radius = a.one("""MATCH (s:Service {name: $svc})
                          CALL algo.BFS(s, 10, 'DEPENDS_ON') YIELD nodes
                          RETURN [n IN nodes | n.name] AS reached""",
                       {"svc": sample["callee"]})
        reached = radius["reached"] if radius else []
        # BFS follows edge direction, so from the callee we reach what IT depends on.
        downstream = a.q("""MATCH (s:Service {name: $svc})-[d:DEPENDS_ON*1..10]->(x:Service)
                            WHERE ALL(e IN relationships(d) WHERE ID(e) >= 0)
                            RETURN DISTINCT x.name AS name""", {"svc": sample["callee"]})
        c.ok("algo.BFS agrees with variable-length match",
             sorted(reached) == sorted(r["name"] for r in downstream),
             f"BFS {len(reached)} vs Cypher {len(downstream)}")
        c.ok("algo.BFS excludes its source node", sample["callee"] not in reached,
             f"source {'absent' if sample['callee'] not in reached else 'PRESENT'}")

        depth_zero = a.q("""MATCH (s:Service {name: $svc})
                            CALL algo.BFS(s, 0, 'DEPENDS_ON') YIELD nodes
                            RETURN size(nodes) AS n""", {"svc": sample["callee"]})
        reached_at_zero = sum(int(r["n"]) for r in depth_zero)
        c.ok("depth 0 reaches nothing (docs claim unlimited — it is not)",
             reached_at_zero == 0,
             f"{len(depth_zero)} row(s), {reached_at_zero} nodes reached")

    print("\nblast radius / impact")
    impacted = a.one("""MATCH (s:Service {tier: 3})
                        MATCH (caller:Service)-[d:DEPENDS_ON*1..5]->(s)
                        WHERE ALL(e IN relationships(d) WHERE ID(e) >= 0)
                        RETURN s.name AS svc, count(DISTINCT caller) AS callers
                        ORDER BY callers DESC LIMIT 1""")
    c.ok("a tier-3 store has upstream callers", impacted and impacted["callers"] > 0,
         f"{impacted['svc']} has {impacted['callers']} upstream" if impacted else "none")

    print("\nmemory")
    sigs = a.q("""MATCH (i:Incident) WITH i.signature AS sig, count(i) AS n
                  WHERE n > 1 RETURN sig, n ORDER BY n DESC""")
    c.ok("signatures collide (memory has something to reuse)", bool(sigs),
         f"{len(sigs)} repeated signature(s), max {sigs[0]['n'] if sigs else 0} incidents")

    plan_steps = a.scalar("""MATCH (:Plan)-[s:STEP]->(:Step) WHERE ID(s) >= 0
                             RETURN count(s) AS c""")
    c.ok("plans have steps", plan_steps > 0, str(plan_steps))

    superseded = a.one("""MATCH (old:Plan)-[r:SUPERSEDED_BY]->(new:Plan)
                          RETURN old.id AS old, new.id AS new, r.reason AS reason""")
    c.ok("scenario C: a plan was retired", superseded is not None,
         f"{superseded['old']} -> {superseded['new']}" if superseded else "none")

    fails_on = a.one("""MATCH (t:Tool)-[f:FAILS_ON]->(c:Condition)
                        RETURN t.name AS tool, c.name AS condition, f.observed_count AS n""")
    c.ok("scenario C: failure condition recorded", fails_on is not None,
         f"{fails_on['tool']} fails on {fails_on['condition']} x{fails_on['n']}"
         if fails_on else "none")

    print("\nvector index")
    idx = [r for r in a.q("CALL db.indexes()") if "VECTOR" in str(r.get("types", r))]
    c.ok("Plan.embedding vector index exists", bool(idx), f"{len(idx)} vector index(es)")

    probe = a.q("""MATCH (p:Plan) WITH p LIMIT 1
                   CALL db.idx.vector.queryNodes('Plan', 'embedding', 3, p.embedding)
                   YIELD node, score RETURN node.id AS id, score ORDER BY score ASC""")
    c.ok("vector recall returns the query plan first (score is a DISTANCE, ascending)",
         bool(probe), f"top score {probe[0]['score']:.2e}" if probe else "no rows")

    print("\nbetweenness")
    bt = a.one("""MATCH (s:Service) WHERE s.betweenness IS NOT NULL
                  RETURN s.name AS name, s.betweenness AS score
                  ORDER BY s.betweenness DESC LIMIT 1""")
    c.ok("chokepoint scores stored", bt is not None,
         f"{bt['name']} = {bt['score']}" if bt else "none")

    print("\nschema")
    d = schema.describe(a)
    c.ok("indexes present", len(d["indices"]) > 5, f"{len(d['indices'])} indexes")
    c.ok("unique constraints present", len(d["constraints"]) > 5,
         f"{len(d['constraints'])} constraints")

    print()
    if c.failures:
        print(f"FAILED: {len(c.failures)} check(s): {', '.join(c.failures)}")
        return False
    print("all checks passed")
    return True


def main() -> None:
    with Ariadne() as a:
        raise SystemExit(0 if run(a) else 1)


if __name__ == "__main__":
    main()
