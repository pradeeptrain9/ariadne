"""The measurement harness — the Track 02 claim, computed.

Runs the same alerts twice: once with memory withheld (`cold`) and once with memory available
(`warm`). Same alerts, same graph, same seed, so the only variable is whether the agent could
recall a plan.

Two guards make the result mean something:

* **accuracy** — a warm run must reach the same culprit commit as its cold counterpart. A cheap
  wrong answer is worse than an expensive right one, so this is asserted rather than eyeballed.
* **determinism** — the graph comes from a fixed seed (`python -m ariadne.seed`, run before this
  harness, not by it), so a re-measurement is against the same world rather than a new one.

Everything printed comes from `runs.jsonl`. No number here is typed by hand.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import ledger as L
from .agent import investigate
from .graph import Ariadne


def pick_alerts(a: Ariadne, n: int) -> list[dict[str, Any]]:
    """Alerts whose shape has a plan behind it — the population where memory can help at all."""
    return a.q("""
        MATCH (i:Incident)-[:SOLVED_BY]->(p:Plan)
        MATCH (i)-[tb:TRIGGERED_BY]->(x:Alert)-[fo:FIRED_ON]->(s:Service)
        WHERE ID(tb) >= 0 AND ID(fo) >= 0
        WITH x, s, p.runs AS runs
        RETURN DISTINCT x.id AS id, x.at AS at, x.metric AS metric, x.value AS value,
               x.severity AS severity, s.name AS service
        ORDER BY runs DESC, id
        LIMIT $n
    """, {"n": int(n)})


def run(n: int, *, ledger: Path, verbose: bool = True) -> dict[str, Any]:
    results: dict[str, dict[str, Any]] = {}

    with Ariadne() as a:
        alerts = pick_alerts(a, n)
        if not alerts:
            raise SystemExit("no alerts with a plan behind them; run `python -m ariadne.seed`")

        for mode in ("cold", "warm"):
            print(f"\n=== {mode} ===")
            for alert in alerts:
                r, _ = investigate(a, alert, ledger=ledger,
                                   force_mode=mode, verbose=verbose)
                results.setdefault(alert["id"], {})[mode] = r

    # accuracy guard
    mismatches = [
        {"alert": aid, "cold": arms["cold"].culprit, "warm": arms["warm"].culprit}
        for aid, arms in results.items()
        if "cold" in arms and "warm" in arms and arms["cold"].culprit != arms["warm"].culprit
    ]

    summary = L.compare(L.load(ledger))
    summary["alerts"] = len(results)
    summary["culprit_mismatches"] = mismatches
    return summary


def report(ledger: Path = L.LEDGER_PATH) -> dict[str, Any]:
    """Aggregate an existing ledger without running anything."""
    return L.compare(L.load(ledger))


def _print(summary: dict[str, Any]) -> None:
    cold, warm = summary.get("cold", {}), summary.get("warm", {})
    print("\n" + "=" * 62)
    print(f"{'':10} {'runs':>5} {'calls':>7} {'tokens':>9} {'cost':>9} {'wall':>8}")
    for name, agg in (("cold", cold), ("warm", warm)):
        if not agg.get("runs"):
            continue
        print(f"{name:10} {agg['runs']:5d} {agg['mean_tool_calls']:7.1f} "
              f"{agg['mean_tokens']:9,d} ${agg['mean_cost_usd']:8.4f} "
              f"{agg['mean_wall_ms']:7,d}ms")
    if "cost_ratio" in summary:
        print(f"\ncost ratio  {summary['cost_ratio']}x cheaper warm")
        print(f"call ratio  {summary['call_ratio']}x fewer tool calls warm")
    mism = summary.get("culprit_mismatches")
    if mism is None:
        pass
    elif mism:
        print(f"\nACCURACY FAILURE: {len(mism)} warm run(s) reached a different culprit")
        for m in mism:
            print(f"  {m['alert']}: cold {m['cold']} vs warm {m['warm']}")
    else:
        print("accuracy    warm runs reached the same culprit as cold on every alert")
    print("=" * 62)


def main() -> None:
    ap = argparse.ArgumentParser(description="Measure cold vs warm cost.")
    ap.add_argument("--n", type=int, default=5, help="how many alerts to run in each arm")
    ap.add_argument("--ledger", type=Path, default=L.LEDGER_PATH)
    ap.add_argument("--report-only", action="store_true", help="aggregate an existing ledger")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.report_only:
        summary = report(args.ledger)
    else:
        if args.ledger.exists():
            args.ledger.unlink()  # a fresh measurement, not an append to an old one
        summary = run(args.n, ledger=args.ledger, verbose=not args.quiet)

    _print(summary)
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ("culprit_mismatches",)}, indent=2))
    if summary.get("culprit_mismatches"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
