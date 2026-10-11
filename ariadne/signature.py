"""The structural signature of an alert.

The signature is the key the procedural memory is indexed by. It is deliberately computed here,
in Python, from graph facts — never asked of the model:

* a model-generated key drifts between runs, and both plan recall and prompt caching depend on
  byte-stability;
* the whole Track 02 claim is a cost number, and an unstable key would quietly destroy it.

Two incidents on completely different services collide on signature when the *situation* has the
same shape — a tier-1 service with high fan-in, a latency alert, a deploy upstream inside the
window. That collision is the retrieval. It is not something similarity over alert text can do:
the alert strings are unrelated; the topology is what matches.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict
from typing import Any

from .graph import Ariadne

#: Controlled vocabulary. An alert metric outside this set is mapped to "other" rather than
#: creating a new signature space per metric name.
METRIC_KINDS = ("latency_p99", "error_rate", "saturation", "queue_depth")

#: Window in which an upstream deploy counts as "recent", in milliseconds.
DEPLOY_WINDOW_MS = 2 * 60 * 60 * 1000

SIGNATURE_LEN = 12


def metric_kind(metric: str) -> str:
    return metric if metric in METRIC_KINDS else "other"


def degree_bucket(n: int) -> str:
    """Bucket a node degree. Buckets, not raw counts, are what make shapes collide."""
    if n == 0:
        return "0"
    if n <= 3:
        return "1-3"
    if n <= 10:
        return "4-10"
    return "11+"


@dataclass(frozen=True)
class Shape:
    """The structural facts behind a signature. Kept alongside the hash so the agent — and a
    judge — can see *why* two incidents matched."""

    metric_kind: str
    tier: int
    fan_in: str
    fan_out: str
    deploy_in_window: bool
    severity: str

    def digest(self) -> str:
        parts = [
            self.metric_kind,
            str(self.tier),
            self.fan_in,
            self.fan_out,
            "deploy" if self.deploy_in_window else "no_deploy",
            self.severity,
        ]
        raw = "|".join(parts).encode()
        return hashlib.sha1(raw).hexdigest()[:SIGNATURE_LEN]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_SHAPE_FACTS = """
MATCH (s:Service {name: $service})
OPTIONAL MATCH (caller:Service)-[cin:DEPENDS_ON]->(s)
  WHERE ID(cin) >= 0
WITH s, count(DISTINCT caller) AS fan_in
OPTIONAL MATCH (s)-[cout:DEPENDS_ON]->(callee:Service)
  WHERE ID(cout) >= 0
WITH s, fan_in, count(DISTINCT callee) AS fan_out
OPTIONAL MATCH (up:Service)<-[deps:DEPENDS_ON*0..2]-(s)
  WHERE ALL(d IN relationships(deps) WHERE ID(d) >= 0)
WITH s, fan_in, fan_out, collect(DISTINCT up.name) AS upstream
OPTIONAL MATCH (d:Deploy)-[t:TOUCHED]->(u:Service)
  WHERE u.name IN upstream AND ID(t) >= 0
    AND d.at <= $at AND d.at >= $at - $window
RETURN s.tier AS tier, fan_in, fan_out, count(d) AS deploys
"""


def shape_of(a: Ariadne, *, service: str, metric: str, severity: str, at: int) -> Shape:
    """Read the structural facts for an alert out of the graph.

    `fan_in` counts services that call this one, `fan_out` those it calls, and `deploys` counts
    deploys that touched this service or anything up to two hops upstream of it inside the
    window. Each aggregation references its edge alias (`ID(e) >= 0`) because FalkorDB otherwise
    only verifies that at least one matching relationship exists, which makes counts wrong.
    """
    row = a.one(_SHAPE_FACTS, {"service": service, "at": at, "window": DEPLOY_WINDOW_MS})
    if row is None:
        raise KeyError(f"unknown service: {service!r}")
    return Shape(
        metric_kind=metric_kind(metric),
        tier=int(row["tier"]),
        fan_in=degree_bucket(int(row["fan_in"])),
        fan_out=degree_bucket(int(row["fan_out"])),
        deploy_in_window=int(row["deploys"]) > 0,
        severity=severity,
    )


def signature_of(a: Ariadne, alert: dict[str, Any]) -> tuple[str, Shape]:
    """Signature plus the shape that produced it, for an alert dict."""
    shape = shape_of(
        a,
        service=alert["service"],
        metric=alert["metric"],
        severity=alert["severity"],
        at=int(alert["at"]),
    )
    return shape.digest(), shape
