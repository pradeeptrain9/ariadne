"""The tool layer, against the real seeded graph. No API key and no model involved.

This is most of the project's correctness surface: if the traversals are right, the agent is
choosing between correct answers.
"""

from __future__ import annotations

import pytest

from ariadne import tools as T
from ariadne.ledger import Run
from ariadne.signature import signature_of


@pytest.fixture
def bound(seeded):
    run = Run(alert_id="test", signature="test", mode="cold", effort="high")
    with T.bind(seeded, run) as ctx:
        yield ctx, run


@pytest.fixture
def alert(seeded):
    return seeded.one("""MATCH (i:Incident)-[:TRIGGERED_BY]->(x:Alert)-[:FIRED_ON]->(s:Service)
                         RETURN x.id AS id, x.at AS at, x.metric AS metric, x.value AS value,
                                x.severity AS severity, s.name AS service LIMIT 1""")


def test_tools_refuse_to_run_unbound():
    with pytest.raises(RuntimeError, match="outside ariadne.tools.bind"):
        T.owner_of("checkout-api")


def test_registry_shape():
    assert len(T.TOOLS) == len(T.TOOL_FUNCS) == 9
    assert "record_outcome" not in T.READ_ONLY
    last = T.TOOLS[-1].to_dict() if hasattr(T.TOOLS[-1], "to_dict") else dict(T.TOOLS[-1])
    assert last["cache_control"] == {"type": "ephemeral"}, "cache breakpoint lost"


def test_every_tool_has_a_description_and_schema():
    for tool in T.TOOLS:
        d = tool.to_dict() if hasattr(tool, "to_dict") else dict(tool)
        assert d["description"].strip(), d["name"]
        assert d["input_schema"]["type"] == "object"


def test_calls_are_recorded_on_the_run(bound):
    _, run = bound
    T.owner_of("checkout-api")
    T.blast_radius("orders-db")
    assert [c.tool for c in run.calls] == ["owner_of", "blast_radius"]
    assert all(c.ok and c.ms >= 0 for c in run.calls)
    assert run.calls[0].order == 0 and run.calls[1].order == 1


def test_streamed_calls_carry_their_own_rows_and_timing(seeded):
    """The demo labels each streamed tool call with a row count and a duration.

    Those are emitted from the tool layer, after the body returns. Emitting them from the
    assistant message's `tool_use` block instead reported the *previous* call's numbers, because
    the tool runner executes a turn's tools after yielding the message that asked for them.
    """
    run = Run(alert_id="test", signature="test", mode="cold", effort="high")
    streamed: list[dict[str, object]] = []
    with T.bind(seeded, run, on_call=lambda c: streamed.append(
            {"tool": c.tool, "rows": c.rows, "ms": c.ms, "order": c.order})):
        T.owner_of("checkout-api")
        T.blast_radius("orders-db")

    assert [e["tool"] for e in streamed] == ["owner_of", "blast_radius"]
    assert [e["order"] for e in streamed] == [0, 1]
    # Every streamed event matches the ledger row for the same call — same tool, same rows.
    assert [(e["tool"], e["rows"]) for e in streamed] == [(c.tool, c.rows) for c in run.calls]
    assert all(e["rows"] > 0 for e in streamed), "a row count attributed to the wrong call"


def test_on_call_fires_without_a_ledger_run(seeded):
    """The hook is independent of the ledger, so the demo can stream a run it does not record."""
    streamed: list[str] = []
    with T.bind(seeded, None, on_call=lambda c: streamed.append(c.tool)):
        T.owner_of("checkout-api")
        T.owner_of("orders-db")
    assert streamed == ["owner_of", "owner_of"]


def test_hops_is_validated_not_interpolated_blindly(bound):
    """Variable-length bounds cannot be parameterised, so the depth is interpolated — which makes
    validating it a correctness requirement, not a nicety."""
    for bad in (0, 6, -1):
        with pytest.raises(ValueError, match="hops must be between"):
            T.upstream_candidates("checkout-api", bad)


def test_tools_return_objects_not_bare_lists(bound):
    """The Messages API rejects a list of row dicts as tool_result content
    (`tool_result.content.0: Input should be an object`), so every tool returns an object."""
    for call in (lambda: T.owner_of("checkout-api"),
                 lambda: T.upstream_candidates("checkout-api", 2),
                 lambda: T.runbook_for("checkout-api"),
                 lambda: T.prior_incidents("ffffffffffff"),
                 lambda: T.blast_radius("orders-db"),
                 lambda: T.recall_plan("ffffffffffff", "orders-db")):
        out = call()
        assert isinstance(out, dict), out


def test_empty_results_explain_themselves(bound):
    """An empty array does not say "nothing shipped, so no commit can be the cause". The note
    does, and that is what stops the agent nominating a commit anyway."""
    out = T.deploys_in_window(["checkout-api"], 1)
    assert out["count"] == 0
    assert "structural" in out["note"]


def test_upstream_candidates_is_ordered_by_the_graph(bound):
    rows = T.upstream_candidates("checkout-api", 3)["rows"]
    assert rows
    keys = [(-int(r["on_critical_path"]), int(r["distance"]), int(r["tier"])) for r in rows]
    assert keys == sorted(keys), "ordering must come from the query, not from the model"


def test_upstream_is_dependencies_not_callers(bound):
    """Direction check at the tool level: a tier-1 API's upstream candidates are things it calls,
    so none of them may be tier 1."""
    rows = T.upstream_candidates("checkout-api", 2)["rows"]
    assert rows and all(int(r["tier"]) > 1 for r in rows)


def test_blast_radius_counts_transitive_callers(bound, seeded):
    hub = seeded.one("""MATCH (caller:Service)-[d:DEPENDS_ON]->(s:Service)
                        WHERE ID(d) >= 0
                        RETURN s.name AS name, count(caller) AS n
                        ORDER BY n DESC LIMIT 1""")
    out = T.blast_radius(hub["name"])
    assert out["impacted_count"] >= hub["n"]
    assert all(int(r["depth"]) >= 1 for r in out["impacted"])


def test_deploys_in_window_can_be_legitimately_empty(bound):
    out = T.deploys_in_window(["checkout-api"], 1)  # epoch 1ms: nothing shipped before that
    assert out["count"] == 0 and out["rows"] == []


def test_suspect_commits_ranks_by_overlap(bound, seeded):
    row = seeded.one("""MATCH (c:Commit)-[:IN]->(d:Deploy)-[t:TOUCHED]->(s:Service)
                        WHERE ID(t) >= 0 RETURN d.id AS deploy, s.name AS service LIMIT 1""")
    out = T.suspect_commits([row["deploy"]], row["service"])["rows"]
    overlaps = [int(r["overlap"]) for r in out]
    assert overlaps == sorted(overlaps, reverse=True)


def test_recall_plan_skips_superseded_plans(bound, seeded):
    """The retired plan must never be the one replayed, and its replacement must have steps —
    a plan with no steps is worse than a miss, because the agent thinks it has guidance."""
    sig = seeded.scalar("MATCH (o:Plan)-[:SUPERSEDED_BY]->(:Plan) RETURN o.signature AS s")
    old = seeded.scalar("MATCH (o:Plan)-[:SUPERSEDED_BY]->(:Plan) RETURN o.id AS i")
    out = T.recall_plan(sig, "catalog-service")
    assert out["found"]
    assert out["plan"] != old
    assert out["hypothesis"], "a hit must carry a verifiable hypothesis"
    assert "blast_radius" in out["already_answered"], "a hit must bundle the prior verdict"


def test_recall_plan_misses_cleanly(bound):
    out = T.recall_plan("ffffffffffff", "orders-db")
    assert out["found"] is False and "investigate" in out["note"]


def test_record_outcome_round_trips_and_is_idempotent(seeded, alert):
    """FalkorDB runs eager writes fully regardless of LIMIT, so the write-back is the one place
    where getting this wrong would corrupt the memory graph."""
    sig, _ = signature_of(seeded, alert)
    iid = "inc-test-roundtrip"
    seeded.write("MATCH (i:Incident {id: $i}) DETACH DELETE i", {"i": iid})

    def once():
        run = Run(alert_id=alert["id"], signature=sig, mode="cold", effort="high")
        with T.bind(seeded, run):
            T.owner_of(alert["service"])
            return T.record_outcome(
                incident_id=iid, alert_id=alert["id"], signature=sig,
                culprit_sha=None, confidence=0.5,
                impacted_services=[alert["service"]], resolved=True,
            )

    first = once()
    assert first["incident"] == iid
    assert first["steps_recorded"] >= 1

    before = seeded.scalar("MATCH (p:Plan {signature: $s}) RETURN p.runs AS r", {"s": sig})
    once()
    after = seeded.scalar("MATCH (p:Plan {signature: $s}) RETURN p.runs AS r", {"s": sig})
    incidents = seeded.scalar("MATCH (i:Incident {id: $i}) RETURN count(i) AS c", {"i": iid})

    assert incidents == 1, "a second call must not create a second incident"
    assert after == before + 1, "plan runs must increment once per call"

    seeded.write("MATCH (i:Incident {id: $i}) DETACH DELETE i", {"i": iid})


def test_record_outcome_accepts_no_culprit(seeded, alert):
    """A structural cause is a valid verdict, and must be recordable as one."""
    sig, _ = signature_of(seeded, alert)
    iid = "inc-test-structural"
    seeded.write("MATCH (i:Incident {id: $i}) DETACH DELETE i", {"i": iid})
    run = Run(alert_id=alert["id"], signature=sig, mode="cold", effort="high")
    with T.bind(seeded, run):
        out = T.record_outcome(
            incident_id=iid, alert_id=alert["id"], signature=sig, culprit_sha=None,
            confidence=0.3, impacted_services=[], resolved=True,
        )
    assert out["culprit"] is None
    caused = seeded.scalar(
        "MATCH (i:Incident {id: $i})-[c:CAUSED_BY]->() RETURN count(c) AS n", {"i": iid})
    assert caused == 0
    seeded.write("MATCH (i:Incident {id: $i}) DETACH DELETE i", {"i": iid})


# --- confidence is computed, never taken from the model ---------------------------------


def test_recorded_confidence_ignores_the_model_s_estimate(seeded, scratch):
    """The project's central claim is that ranking is computed, not asked of a model. Confidence
    is the one number in the verdict a model could plausibly have been left to invent, so the
    write path recomputes it and keeps the model's estimate only for comparison."""
    alert = seeded.one("""MATCH (i:Incident)-[:CAUSED_BY]->(c:Commit)
                          MATCH (i)-[:TRIGGERED_BY]->(x:Alert)
                          RETURN x.id AS alert, c.sha AS sha LIMIT 1""")
    expected, facts = T.computed_confidence(seeded, alert_id=alert["alert"], sha=alert["sha"])

    run = Run(alert_id=alert["alert"], signature="sig-conf", mode="cold", effort="high")
    with T.bind(seeded, run):
        out = T.record_outcome(
            incident_id="inc-conf-test", alert_id=alert["alert"], signature="sig-conf",
            culprit_sha=alert["sha"], confidence=0.01, impacted_services=[],
        )
    try:
        row = seeded.one("""MATCH (:Incident {id: 'inc-conf-test'})-[cb:CAUSED_BY]->(:Commit)
                            RETURN cb.confidence AS stored, cb.model_confidence AS model,
                                   cb.overlap AS overlap, cb.files_changed AS files,
                                   cb.distance_hops AS distance""")
        assert row["stored"] == pytest.approx(expected)
        assert row["stored"] != pytest.approx(0.01), "the model's estimate was stored"
        assert row["model"] == pytest.approx(0.01), "the model's estimate was not kept"
        assert (row["overlap"], row["files"], row["distance"]) == (
            facts["overlap"], facts["files_changed"], facts["distance"])
        # Handed back so the verdict can quote the computed number instead of its own guess.
        assert out["confidence"] == pytest.approx(expected)
        assert out["your_estimate_was"] == pytest.approx(0.01)
    finally:
        seeded.write("MATCH (i:Incident {id: 'inc-conf-test'}) DETACH DELETE i")
        seeded.write("MATCH (p:Plan {signature: 'sig-conf'}) DETACH DELETE p")
        seeded.write("MATCH (s:Step) WHERE s.id STARTS WITH 'inc-conf-test' DETACH DELETE s")


def test_seeded_confidences_are_computed_not_random(seeded):
    """The historical incidents are what `recall_plan` and `prior_incidents` hand the model as
    evidence. A seeded `uniform(0.62, 0.93)` would make those the only numbers in the graph with
    no arithmetic behind them."""
    rows = seeded.q("""MATCH (i:Incident)-[cb:CAUSED_BY]->(c:Commit)
                       MATCH (i)-[:TRIGGERED_BY]->(x:Alert)
                       RETURN x.id AS alert, c.sha AS sha, cb.confidence AS stored""")
    assert rows, "no seeded incident has a culprit"
    for r in rows:
        expected, _ = T.computed_confidence(seeded, alert_id=r["alert"], sha=r["sha"])
        assert r["stored"] == pytest.approx(expected), r["alert"]


def test_distance_term_is_zero_only_when_the_deploy_hit_the_failing_service(seeded):
    facts = seeded.q("""MATCH (i:Incident)-[cb:CAUSED_BY]->(c:Commit)-[ci:IN]->(d:Deploy)
                        MATCH (i)-[:TRIGGERED_BY]->(x:Alert)-[:FIRED_ON]->(f:Service)
                        WHERE ID(cb) >= 0 AND ID(ci) >= 0
                        RETURN x.id AS alert, c.sha AS sha, f.name AS failing LIMIT 20""")
    for r in facts:
        got = T.confidence_facts(seeded, alert_id=r["alert"], sha=r["sha"])
        if got["distance"] == 0:
            assert r["failing"] in got["touched"]
        else:
            assert r["failing"] not in got["touched"]


def test_blast_radius_hop_cap_covers_the_whole_mesh(seeded):
    """`blast_radius` traverses `*1..5`, while the demo page walks the callers unbounded. They
    agree only while no dependency chain is longer than the cap; a deeper mesh would make the
    drawing and the verdict disagree silently, so the cap is pinned to the graph's diameter."""
    longest = seeded.scalar("""MATCH (c:Service)-[deps:DEPENDS_ON*1..8]->(:Service)
                               WHERE ALL(d IN relationships(deps) WHERE ID(d) >= 0)
                               RETURN max(length(deps)) AS d""")
    assert int(longest) <= 5, (
        f"longest dependency chain is {longest} hops but blast_radius only follows 5")
