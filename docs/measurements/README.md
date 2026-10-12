# Measurements

Raw ledger: `runs.jsonl` in this directory, written by `python -m ariadne.replay` and
by runs driven from the demo page. Every number in the README and the submission comes
from this file; none is typed by hand.

Model **`claude-opus-5`** — every run below was made on Opus 5, and each row in `runs.jsonl`
now records its own model so a mixed ledger can never be averaged into a meaningless ratio
(`ledger.compare` emits a warning instead).

**The project has since been switched to `claude-sonnet-5`** for cost. These numbers have not
been re-measured on Sonnet 5 yet, so read them as Opus 5 results. The ratio is the claim and it
should hold across models, but "should" is not "measured" and this file only contains what was
actually run.

Cold = memory withheld, warm = memory available, **same alerts** in both arms against the same
deterministic graph.

| mode | runs | tool calls | tokens | cost | wall |
|---|---|---|---|---|---|
| cold | 4 | 8.0 | 36,017 | $0.1281 | 32.9s |
| warm | 6 | 4.0 | 25,288 | $0.0677 | 19.3s |

**1.9x cheaper, 2.0x fewer tool calls when memory is available.**

## Per run

| mode | alert | calls | tokens | cache read | cost | culprit | confidence |
|---|---|---|---|---|---|---|---|
| cold | alr-055 | 8 | 35,585 | 17,980 | $0.1367 | `951e8b261e` | 0.55 |
| cold | alr-a00 | 8 | 36,211 | 21,576 | $0.1187 | `614ca4f7a5` | 0.5 |
| cold | alr-a01 | 8 | 37,109 | 21,576 | $0.1229 | `d524715b1f` | 0.45 |
| warm | alr-055 | 4 | 25,680 | 14,384 | $0.0870 | `951e8b261e` | 0.72 |
| warm | alr-a00 | 4 | 25,400 | 17,980 | $0.0651 | `614ca4f7a5` | 0.8 |
| warm | alr-a01 | 4 | 24,395 | 17,980 | $0.0575 | `d524715b1f` | 0.75 |
| warm | alr-a08 | 4 | 25,447 | 17,980 | $0.0662 | `402a6ec978` | 0.75 |
| warm | alr-a08 | 4 | 25,384 | 17,980 | $0.0648 | `402a6ec978` | 0.8 |
| cold | alr-a08 | 8 | 35,163 | 17,980 | $0.1342 | `402a6ec978` | 0.75 |
| warm | alr-a08 | 4 | 25,421 | 17,980 | $0.0656 | `402a6ec978` | 0.8 |

## Accuracy guard

4 of 4 alerts run in both arms reached the **same culprit commit**.
A cheap wrong answer is worse than an expensive right one, so `replay.py` asserts this
rather than leaving it to inspection.

## Tool usage by mode

- **cold**: `upstream_candidates`, `prior_incidents`, `deploys_in_window`, `suspect_commits`, `blast_radius`, `owner_of`, `runbook_for`, `record_outcome`
- **warm**: `recall_plan`, `deploys_in_window`, `suspect_commits`, `record_outcome`

Cold explores (`upstream_candidates`, `prior_incidents`, `blast_radius`,
`owner_of`, `runbook_for`). Warm verifies a hypothesis the graph already holds, so it
runs `recall_plan` then only `deploys_in_window` and `suspect_commits` before
recording.
