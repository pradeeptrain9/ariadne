"""Pure functions: the confidence number and the cost arithmetic.

Both are things a judge may ask us to justify on the spot, so both are pinned.
"""

from __future__ import annotations

import json

from ariadne.ledger import Run, ToolCall, compare, cost_usd
from ariadne.scoring import FLOOR, CEILING, confidence, explain


def test_confidence_is_bounded():
    assert confidence(overlap=99, files_changed=1, distance=1,
                      deploy_at=0, alert_at=0) == CEILING
    assert confidence(overlap=0, files_changed=50, distance=5,
                      deploy_at=0, alert_at=10**12) >= FLOOR


def test_overlap_dominates():
    """Path overlap carries the most weight: it is the most direct evidence in the graph."""
    high = confidence(overlap=4, files_changed=4, distance=2, deploy_at=0, alert_at=60_000)
    low = confidence(overlap=0, files_changed=4, distance=2, deploy_at=0, alert_at=60_000)
    assert high > low


def test_shared_library_commit_scores_low():
    """Scenario C's condition: the ranking under-scores shared-library causes, and the agent is
    told to say so rather than silently promoting one."""
    assert confidence(overlap=0, files_changed=3, distance=1,
                      deploy_at=0, alert_at=10 * 60_000) < 0.5


def test_stale_deploy_scores_lower_than_recent():
    recent = confidence(overlap=2, files_changed=4, distance=1, deploy_at=0, alert_at=60_000)
    stale = confidence(overlap=2, files_changed=4, distance=1,
                       deploy_at=0, alert_at=5 * 60 * 60_000)
    assert recent > stale


def test_explain_exposes_every_term():
    out = explain(overlap=3, files_changed=4, distance=2, deploy_at=0, alert_at=12 * 60_000)
    assert out["confidence"] == confidence(overlap=3, files_changed=4, distance=2,
                                           deploy_at=0, alert_at=12 * 60_000)
    assert set(out["terms"]) == {"path_overlap", "distance", "deploy_to_alert_minutes"}
    assert set(out["weights"]) == {"overlap", "distance", "recency"}


def test_cost_arithmetic():
    # 1M input at $5 plus 1M output at $25
    assert cost_usd(input_tokens=1_000_000, output_tokens=0) == 5.0
    assert cost_usd(input_tokens=0, output_tokens=1_000_000) == 25.0
    # cache reads are a tenth of input
    assert cost_usd(input_tokens=0, output_tokens=0, cache_read=1_000_000) == 0.5


class _Usage:
    def __init__(self, i, o, cw=0, cr=0):
        self.input_tokens, self.output_tokens = i, o
        self.cache_creation_input_tokens, self.cache_read_input_tokens = cw, cr


def test_run_accumulates_across_turns():
    r = Run(alert_id="a", signature="s", mode="cold", effort="high")
    r.add_usage(_Usage(1000, 100))
    r.add_usage(_Usage(1200, 150, cr=800))
    r.add_call(ToolCall(order=0, tool="owner_of", args={}, rows=2, ms=3))
    assert r.turns == 2 and r.input_tokens == 2200 and r.cache_read == 800
    assert r.tool_calls == 1
    assert r.tokens == 2200 + 250 + 800
    assert json.loads(json.dumps(r.as_dict()))["cost_usd"] > 0


def test_tool_call_intent_is_human_readable():
    assert "memory" in ToolCall(order=0, tool="recall_plan", args={}, rows=1, ms=1).intent()
    assert ToolCall(order=0, tool="unknown_tool", args={}, rows=0, ms=0).intent() == "unknown tool"


def test_compare_computes_the_ratio():
    rows = [
        {"mode": "cold", "tool_calls": 12, "total_tokens": 30000, "cost_usd": 0.40, "wall_ms": 9000},
        {"mode": "cold", "tool_calls": 14, "total_tokens": 34000, "cost_usd": 0.44, "wall_ms": 9500},
        {"mode": "warm", "tool_calls": 4, "total_tokens": 9000, "cost_usd": 0.07, "wall_ms": 2500},
        {"mode": "warm", "tool_calls": 3, "total_tokens": 8000, "cost_usd": 0.06, "wall_ms": 2300},
    ]
    out = compare(rows)
    assert out["cold"]["runs"] == 2 and out["warm"]["runs"] == 2
    assert out["cost_ratio"] == round(0.42 / 0.065, 1)
    assert out["call_ratio"] == round(13.0 / 3.5, 1)


def test_compare_handles_one_sided_ledgers():
    out = compare([{"mode": "cold", "tool_calls": 9, "total_tokens": 1, "cost_usd": 0.1,
                    "wall_ms": 1}])
    assert out["warm"]["runs"] == 0
    assert "cost_ratio" not in out
