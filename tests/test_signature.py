"""The signature is the key the whole memory is indexed by, so its stability is load-bearing."""

from __future__ import annotations

from ariadne.signature import Shape, degree_bucket, metric_kind, signature_of

BASE = dict(metric_kind="latency_p99", tier=1, fan_in="4-10", fan_out="1-3",
            deploy_in_window=True, severity="high")


def test_digest_is_deterministic():
    assert Shape(**BASE).digest() == Shape(**BASE).digest()


def test_digest_is_stable_across_processes():
    # sha1 of a joined string, not Python's salted hash() — a restart must not change the key,
    # or every plan in the graph becomes unreachable.
    assert Shape(**BASE).digest() == "a965ed3033c4"


def test_every_field_changes_the_digest():
    base = Shape(**BASE).digest()
    for field, other in (
        ("metric_kind", "error_rate"), ("tier", 3), ("fan_in", "11+"),
        ("fan_out", "0"), ("deploy_in_window", False), ("severity", "low"),
    ):
        assert Shape(**{**BASE, field: other}).digest() != base, field


def test_degree_buckets():
    assert [degree_bucket(n) for n in (0, 1, 3, 4, 10, 11, 500)] == \
        ["0", "1-3", "1-3", "4-10", "4-10", "11+", "11+"]


def test_unknown_metric_collapses_to_other():
    assert metric_kind("latency_p99") == "latency_p99"
    assert metric_kind("gc_pause_p999") == "other"


def test_signature_from_graph_matches_shape(seeded):
    alert = seeded.one("""MATCH (x:Alert)-[:FIRED_ON]->(s:Service)
                          RETURN x.id AS id, x.metric AS metric, x.severity AS severity,
                                 x.at AS at, s.name AS service LIMIT 1""")
    sig, shape = signature_of(seeded, {
        "service": alert["service"], "metric": alert["metric"],
        "severity": alert["severity"], "at": alert["at"],
    })
    assert sig == shape.digest()
    assert len(sig) == 12
