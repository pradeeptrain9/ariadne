"""Confidence, computed.

The number the agent reports for "this commit caused it" is arithmetic over graph results, not a
model judgement. Written down in one place so it means the same thing on every run, and so that
"where did 0.78 come from" has an answer a judge can check.

Weights are a design choice, not a measurement — stated plainly rather than dressed up. What
matters is that every *input* is a fact read from the graph.
"""

from __future__ import annotations

W_OVERLAP = 0.55   # how much of the commit landed in the failing service's own paths
W_DISTANCE = 0.25  # how many dependency hops away the change was
W_RECENCY = 0.20   # whether the deploy landed just before the alert

FLOOR, CEILING = 0.1, 0.95
RECENT_MS = 30 * 60 * 1000


def clamp(value: float, low: float = FLOOR, high: float = CEILING) -> float:
    return max(low, min(high, value))


def confidence(
    *,
    overlap: int,
    files_changed: int,
    distance: int,
    deploy_at: int,
    alert_at: int,
) -> float:
    """Confidence that a commit caused an alert.

    Args:
        overlap: changed files falling under the failing service's owned path prefixes.
        files_changed: total files in the commit.
        distance: dependency hops from the deployed service to the failing one (0 = same service).
        deploy_at: deploy timestamp, epoch ms.
        alert_at: alert timestamp, epoch ms.
    """
    overlap_term = overlap / max(1, files_changed)
    distance_term = 1.0 / max(1, distance)
    recent = (alert_at - deploy_at) <= RECENT_MS
    recency_term = 1.0 if recent else 0.4
    raw = W_OVERLAP * overlap_term + W_DISTANCE * distance_term + W_RECENCY * recency_term
    return round(clamp(raw), 2)


def explain(
    *, overlap: int, files_changed: int, distance: int, deploy_at: int, alert_at: int
) -> dict[str, object]:
    """The same computation with its terms exposed, for the verdict and the demo."""
    return {
        "confidence": confidence(
            overlap=overlap, files_changed=files_changed, distance=distance,
            deploy_at=deploy_at, alert_at=alert_at,
        ),
        "terms": {
            "path_overlap": f"{overlap}/{files_changed} files in the failing service's paths",
            "distance": f"{distance} dependency hop(s) away",
            "deploy_to_alert_minutes": round((alert_at - deploy_at) / 60000, 1),
        },
        "weights": {"overlap": W_OVERLAP, "distance": W_DISTANCE, "recency": W_RECENCY},
    }
