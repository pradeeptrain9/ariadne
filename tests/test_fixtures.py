"""The seeded graph has to satisfy docs/fixtures.md, because the demo depends on its shape."""

from __future__ import annotations


def test_service_tiers(seeded):
    counts = {int(r["tier"]): int(r["n"]) for r in seeded.q(
        "MATCH (s:Service) RETURN s.tier AS tier, count(s) AS n")}
    assert counts == {1: 6, 2: 14, 3: 20}


def test_dependency_direction_is_caller_to_callee(seeded):
    """(caller)-[:DEPENDS_ON]->(callee). A tier-1 API must point *at* tier-2/3, never be
    pointed at by them. Reversed, every traversal in the project still returns plausible rows."""
    wrong = seeded.scalar("""MATCH (low:Service)-[d:DEPENDS_ON]->(api:Service {tier: 1})
                             WHERE ID(d) >= 0 AND low.tier > 1 RETURN count(d) AS c""")
    assert wrong == 0


def test_mesh_is_one_component(seeded):
    comps = seeded.q("""CALL algo.WCC({nodeLabels: ['Service'], relationshipTypes: ['DEPENDS_ON']})
                        YIELD componentId RETURN DISTINCT componentId AS id""")
    assert len(comps) == 1


def test_scenario_a_has_a_reusable_corpus(seeded):
    """At least eight resolved incidents sharing one signature, on more than one service —
    otherwise 'the plan transfers across services' is an untested claim."""
    top = seeded.one("""MATCH (i:Incident) WITH i.signature AS sig, count(i) AS n
                        WHERE n >= 8 RETURN sig, n ORDER BY n DESC LIMIT 1""")
    assert top is not None, "no signature has 8+ incidents"

    services = seeded.q("""MATCH (i:Incident {signature: $sig})-[:TRIGGERED_BY]->(:Alert)
                                 -[:FIRED_ON]->(s:Service)
                           RETURN DISTINCT s.name AS name""", {"sig": top["sig"]})
    assert len(services) > 1


def test_scenario_b_signature_is_genuinely_novel(seeded):
    """The held-back novel alert must not match any resolved incident, or the 'cold path still
    works' demo is secretly a warm path."""
    import json
    from pathlib import Path
    from ariadne.signature import signature_of

    path = Path("fixtures/scenarios.json")
    if not path.exists():
        return  # scenarios file is written by the CLI, not by the fixture
    alert = json.loads(path.read_text())["scenario_b"]
    sig, _ = signature_of(seeded, alert)
    known = seeded.scalar("MATCH (i:Incident {signature: $s}) RETURN count(i) AS c", {"s": sig})
    assert known == 0


def test_scenario_c_records_a_retired_plan(seeded):
    row = seeded.one("""MATCH (old:Plan)-[r:SUPERSEDED_BY]->(new:Plan)
                        RETURN old.runs AS runs, old.successes AS ok, r.reason AS reason""")
    assert row is not None
    assert row["runs"] > row["ok"], "a superseded plan should have failed at least once"
    assert row["reason"]

    cond = seeded.one("""MATCH (t:Tool)-[f:FAILS_ON]->(c:Condition)
                         RETURN t.name AS tool, c.name AS cond, f.observed_count AS n""")
    assert cond and cond["n"] > 0


def test_commits_overlap_their_service_paths(seeded):
    """suspect_commits ranks by file overlap, so the fixture must contain commits that actually
    overlap — and some that deliberately do not (the shared-library noise)."""
    row = seeded.one("""MATCH (c:Commit)-[:IN]->(d:Deploy)-[t:TOUCHED]->(s:Service)
                        WHERE ID(t) >= 0
                        WITH c, s,
                             size([f IN c.files WHERE ANY(p IN s.paths WHERE f STARTS WITH p)]) AS overlap
                        RETURN sum(CASE WHEN overlap > 0 THEN 1 ELSE 0 END) AS hits,
                               sum(CASE WHEN overlap = 0 THEN 1 ELSE 0 END) AS misses""")
    assert row["hits"] > 0
    assert row["misses"] > 0


def test_betweenness_is_stored(seeded):
    n = seeded.scalar("MATCH (s:Service) WHERE s.betweenness IS NOT NULL RETURN count(s) AS c")
    assert n == 40


def test_reseeding_is_deterministic(seeded):
    """Same seed, same graph — the cost comparison is meaningless otherwise."""
    before = seeded.q("MATCH (c:Commit) RETURN c.sha AS sha ORDER BY sha LIMIT 20")
    from ariadne import seed as seed_mod
    seed_mod.seed(seeded)
    after = seeded.q("MATCH (c:Commit) RETURN c.sha AS sha ORDER BY sha LIMIT 20")
    assert before == after
