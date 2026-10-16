"""Pure functions: the confidence number and the cost arithmetic.

Both are things a judge may ask us to justify on the spot, so both are pinned.
"""

from __future__ import annotations

import json

import pytest

from ariadne import ledger as L
from ariadne.ledger import PRICES, Run, ToolCall, compare, cost_usd
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
    assert cost_usd(input_tokens=1_000_000, output_tokens=0, model="claude-sonnet-5") == 2.0
    assert cost_usd(input_tokens=0, output_tokens=1_000_000, model="claude-sonnet-5") == 10.0
    # cache reads are a tenth of input
    assert cost_usd(input_tokens=0, output_tokens=0, cache_read=1_000_000,
                    model="claude-sonnet-5") == 0.2


def test_prices_exist_for_the_configured_model():
    """A ledger that cannot price the model it ran on produces numbers that mean nothing."""
    from ariadne.agent import MODEL
    assert MODEL in PRICES, f"{MODEL} has no prices in ledger.PRICES"


def test_cache_prices_follow_the_published_multipliers():
    for model, p in PRICES.items():
        assert p["cache_write"] == pytest.approx(p["input"] * 1.25), model
        assert p["cache_read"] == pytest.approx(p["input"] * 0.1), model


def test_unknown_model_refuses_to_guess():
    with pytest.raises(KeyError, match="no prices recorded"):
        cost_usd(input_tokens=1, output_tokens=1, model="some-new-model")


def test_runs_carry_their_model():
    r = Run(alert_id="a", signature="s", mode="cold", effort="high", model="claude-opus-5")
    r.add_usage(_Usage(1_000_000, 0))
    assert r.cost == 5.0
    assert r.as_dict()["model"] == "claude-opus-5"


def test_compare_flags_a_ledger_that_mixes_models():
    """Averaging a cheap model against an expensive one is not a memory measurement."""
    rows = [
        {"mode": "cold", "model": "claude-opus-5", "tool_calls": 8, "total_tokens": 1,
         "cost_usd": 0.13, "wall_ms": 1},
        {"mode": "warm", "model": "claude-sonnet-5", "tool_calls": 4, "total_tokens": 1,
         "cost_usd": 0.03, "wall_ms": 1},
    ]
    out = compare(rows)
    assert out["models"] == ["claude-opus-5", "claude-sonnet-5"]
    assert "warning" in out and "mixes models" in out["warning"]


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


def test_verdict_keeps_every_text_block():
    """The model often writes the verdict before record_outcome and a short closer after it.
    Keeping only the last block showed "Summary above stands" and dropped the actual verdict."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "ariadne" / "agent.py"
    tree = ast.parse(src.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "investigate")
    body = ast.unparse(fn)
    assert "texts.append" in body, "text blocks must be accumulated"
    assert "join(texts)" in body, "the verdict must join every block, not take the last"


# --- demo ledger source ---------------------------------------------------------------


def test_demo_prefers_a_local_ledger_and_falls_back_to_the_shipped_one(tmp_path, monkeypatch):
    """The cold-vs-warm panel must never open empty on a fresh clone, and must never present the
    committed measurements as if they had been produced on this machine."""
    from ariadne import server

    monkeypatch.chdir(tmp_path)

    source, rows = server.ledger_rows()
    assert source == server.SHIPPED_LEDGER, "no local ledger: should read the committed runs"
    assert rows, "the repo ships measurements; the fallback found none"
    assert {r["mode"] for r in rows} == {"cold", "warm"}

    (tmp_path / "runs.jsonl").write_text(json.dumps({
        "alert_id": "a", "signature": "s", "mode": "cold", "effort": "high",
        "model": "claude-sonnet-5", "tool_calls": 7, "total_tokens": 1,
        "cost_usd": 0.01, "wall_ms": 1,
    }) + "\n")
    source, rows = server.ledger_rows()
    assert source == L.LEDGER_PATH and len(rows) == 1, "a local run must win over the shipped file"


def test_shipped_measurements_match_the_published_summary():
    """`docs/measurements/summary.json` is generated from `runs.jsonl`; a hand-edited number
    there would be exactly the kind of typed-in figure this project claims never to have."""
    from ariadne import server

    rows = L.load(server.SHIPPED_LEDGER)
    published = json.loads(
        (server.SHIPPED_LEDGER.parent / "summary.json").read_text()
    )
    computed = L.compare(rows)
    for key in ("cold", "warm", "cost_ratio", "call_ratio", "models"):
        assert computed[key] == published[key], key
