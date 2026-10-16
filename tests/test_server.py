"""The demo server's data routes, against the real seeded graph.

The page is what a judge actually looks at, and everything on it is served from here. The
computed verdict in particular has to agree with the graph rather than with the model's prose —
that is the whole claim the demo is making.
"""

from __future__ import annotations

import pytest

from ariadne import tools as T
from ariadne.ledger import Run
from ariadne.server import graph_payload, verdict_payload


@pytest.fixture
def incident(seeded):
    return seeded.one("""MATCH (i:Incident)-[cb:CAUSED_BY]->(c:Commit)
                         WHERE ID(cb) >= 0
                         RETURN i.id AS id, c.sha AS sha LIMIT 1""")


def test_unknown_incident_has_no_verdict(seeded):
    assert verdict_payload(seeded, "inc-does-not-exist") is None


def test_verdict_confidence_is_the_computed_one(seeded, incident):
    v = verdict_payload(seeded, incident["id"])
    expected, facts = T.computed_confidence(
        seeded, alert_id=v["alert"]["id"], sha=incident["sha"])
    assert v["culprit"]["sha"] == incident["sha"]
    assert v["culprit"]["confidence"] == pytest.approx(expected)
    assert v["culprit"]["distance_hops"] == facts["distance"]
    terms = v["culprit"]["terms"]["terms"]
    assert f"{facts['overlap']}/{facts['files_changed']}" in terms["path_overlap"]


def test_verdict_impact_agrees_with_the_blast_radius_tool(seeded, incident):
    v = verdict_payload(seeded, incident["id"])
    with T.bind(seeded):
        tool = T.blast_radius(v["failing"])
    assert v["impacted_count"] == tool["impacted_count"]
    assert v["tier1_impacted"] == tool["tier1_impacted"]
    assert {r["service"] for r in v["impacted"]} == {r["service"] for r in tool["impacted"]}


def test_verdict_cone_is_what_upstream_candidates_returned(seeded, incident):
    """The page draws this set as "searched for the cause". Recomputing it in the browser would
    claim a wider search than the tool's capped result actually made."""
    v = verdict_payload(seeded, incident["id"])
    with T.bind(seeded):
        tool = T.upstream_candidates(v["failing"], 3)
    assert v["cone"] == [r["service"] for r in tool["rows"]]


def test_a_structural_verdict_has_no_culprit(seeded):
    """`culprit_sha: null` is a real answer — nothing shipped, so the cause is structural. The
    card must render that rather than an empty commit row."""
    alert = seeded.one("""MATCH (x:Alert)-[:FIRED_ON]->(:Service)
                          RETURN x.id AS id ORDER BY x.id DESC LIMIT 1""")
    run = Run(alert_id=alert["id"], signature="sig-structural", mode="cold", effort="high")
    with T.bind(seeded, run):
        T.record_outcome(incident_id="inc-structural-test", alert_id=alert["id"],
                         signature="sig-structural", culprit_sha=None, confidence=0.0,
                         impacted_services=[])
    try:
        v = verdict_payload(seeded, "inc-structural-test")
        assert v is not None and v["culprit"] is None
        assert v["failing"]
    finally:
        seeded.write("MATCH (i:Incident {id: 'inc-structural-test'}) DETACH DELETE i")
        seeded.write("MATCH (p:Plan {signature: 'sig-structural'}) DETACH DELETE p")
        seeded.write("MATCH (s:Step) WHERE s.id STARTS WITH 'inc-structural-test' "
                     "DETACH DELETE s")


def test_graph_payload_draws_every_service_and_edge(seeded):
    """The page lays out whatever this returns; a missing endpoint would silently drop an edge."""
    g = graph_payload(seeded)
    names = {s["name"] for s in g["services"]}
    assert len(names) == g["stats"]["service"] == 40
    for d in g["deps"]:
        assert d["source"] in names and d["target"] in names
    assert len(g["deps"]) == 100
