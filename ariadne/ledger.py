"""Usage and cost accounting.

Every turn's `usage` is appended to a JSONL file and the cold-vs-warm chart is generated from it.
No number in the submission is typed by hand — that is the point of this module.

Prices are Claude Opus 5 list rates. They are constants here rather than fetched, so a reader can
see exactly what the dollar figures were computed with.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

#: USD per million tokens. Cache write is 1.25x input, cache read 0.1x input.
#: Listed per model rather than as four module constants, because a ledger that does not say
#: which model produced a number is not evidence of anything — and the first version of this
#: file had Opus prices hardcoded while the model id lived in another module.
PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5":   {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10},
}

DEFAULT_MODEL = "claude-sonnet-5"
LEDGER_PATH = Path("runs.jsonl")


def prices_for(model: str) -> dict[str, float]:
    try:
        return PRICES[model]
    except KeyError:
        raise KeyError(
            f"no prices recorded for {model!r}; add it to ledger.PRICES so costs stay real"
        ) from None


def cost_usd(
    *, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0,
    model: str = DEFAULT_MODEL,
) -> float:
    p = prices_for(model)
    return (
        input_tokens * p["input"]
        + output_tokens * p["output"]
        + cache_write * p["cache_write"]
        + cache_read * p["cache_read"]
    ) / 1_000_000


@dataclass
class ToolCall:
    """One tool execution. Becomes a Step/Result pair in the memory graph."""

    order: int
    tool: str
    args: dict[str, Any]
    rows: int
    ms: int
    ok: bool = True
    error: str | None = None

    def intent(self) -> str:
        return INTENTS.get(self.tool, self.tool.replace("_", " "))


#: Human-readable intent per tool, stored on Step nodes so a replayed plan reads as a plan
#: rather than a list of function names.
INTENTS = {
    "recall_plan": "check memory for this situation's shape",
    "upstream_candidates": "narrow the dependency cone",
    "deploys_in_window": "find changes in the window",
    "suspect_commits": "rank commits by file overlap",
    "blast_radius": "measure downstream impact",
    "owner_of": "identify the on-call owner",
    "runbook_for": "attach the matching runbook",
    "prior_incidents": "look up past incidents of this shape",
    "record_outcome": "write the outcome back to memory",
}


@dataclass
class Run:
    """One investigation, cold or warm."""

    alert_id: str
    signature: str
    mode: str  # "cold" | "warm"
    effort: str
    model: str = DEFAULT_MODEL
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write: int = 0
    cache_read: int = 0
    turns: int = 0
    calls: list[ToolCall] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    wall_ms: int = 0
    culprit: str | None = None
    confidence: float | None = None
    impacted: int = 0

    # --- accumulation ---------------------------------------------------------

    def add_usage(self, usage: Any) -> None:
        """Accumulate one API response's usage."""
        self.turns += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0

    def add_call(self, call: ToolCall) -> None:
        self.calls.append(call)

    def finish(self) -> "Run":
        self.wall_ms = int((time.time() - self.started) * 1000)
        return self

    # --- reporting ------------------------------------------------------------

    @property
    def tool_calls(self) -> int:
        return len(self.calls)

    @property
    def cost(self) -> float:
        return cost_usd(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_write=self.cache_write,
            cache_read=self.cache_read,
            model=self.model,
        )

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_write

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["calls"] = [asdict(c) for c in self.calls]
        d["cost_usd"] = round(self.cost, 6)
        d["tool_calls"] = self.tool_calls
        d["total_tokens"] = self.tokens
        d.pop("started", None)
        return d

    def append_to(self, path: Path = LEDGER_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(self.as_dict()) + "\n")

    def summary(self) -> str:
        return (
            f"{self.mode:4} {self.model} effort={self.effort:4} "
            f"{self.tool_calls:2d} tool calls  {self.turns:2d} turns  "
            f"{self.tokens:7,d} tok  (cache read {self.cache_read:,d})  "
            f"${self.cost:.4f}  {self.wall_ms:,d}ms"
        )


def load(path: Path = LEDGER_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def compare(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Cold-vs-warm aggregate. This is the Track 02 claim, computed from the ledger."""
    cold = [r for r in runs if r["mode"] == "cold"]
    warm = [r for r in runs if r["mode"] == "warm"]

    def agg(rows: list[dict[str, Any]]) -> dict[str, float]:
        if not rows:
            return {"runs": 0}
        n = len(rows)
        return {
            "runs": n,
            "mean_tool_calls": round(sum(r["tool_calls"] for r in rows) / n, 1),
            "mean_tokens": round(sum(r["total_tokens"] for r in rows) / n),
            "mean_cost_usd": round(sum(r["cost_usd"] for r in rows) / n, 4),
            "mean_wall_ms": round(sum(r["wall_ms"] for r in rows) / n),
        }

    c, w = agg(cold), agg(warm)
    models = sorted({r.get("model", "unknown") for r in runs})
    out: dict[str, Any] = {"cold": c, "warm": w, "models": models}
    if len(models) > 1:
        # Mixing models makes the ratio meaningless — say so rather than printing a number.
        out["warning"] = f"ledger mixes models {models}; re-run both arms on one model"
    if c.get("runs") and w.get("runs") and w["mean_cost_usd"]:
        out["cost_ratio"] = round(c["mean_cost_usd"] / w["mean_cost_usd"], 1)
        out["call_ratio"] = round(c["mean_tool_calls"] / max(0.1, w["mean_tool_calls"]), 1)
    return out
