"""`docs/queries.md` must contain the queries the code actually runs.

Rule 10 requires the submission to ship "the Cypher queries or graph algorithms the product
depends on". That document was originally written *before* the code, and drifted: it described a
vector-recall fallback that was never wired in, an `algo.BFS` blast radius that the code does not
use, and `WHERE NOT EXISTS(...)` forms that FalkorDB rejects outright.

A document that disagrees with the code is worse than no document, so the agreement is a test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "queries.md"
SOURCES = ("ariadne/tools.py", "ariadne/signature.py")

#: f-string interpolation in the source becomes this placeholder in the doc.
PLACEHOLDER = "{…}"


def norm(text: str) -> str:
    """Collapse whitespace so indentation and wrapping do not count as drift."""
    return " ".join(text.split())


def source_blob() -> str:
    return norm("\n".join((ROOT / p).read_text() for p in SOURCES))


def doc_queries() -> list[str]:
    blocks = re.findall(r"```cypher\n(.*?)```", DOC.read_text(), re.S)
    return [b for b in blocks if b.strip()]


def strip_comments(q: str) -> str:
    return "\n".join(line.split("--")[0] for line in q.splitlines())


@pytest.fixture(scope="module")
def blob() -> str:
    return source_blob()


def test_the_doc_has_queries():
    assert len(doc_queries()) >= 15, "queries.md lost its code blocks"


@pytest.mark.parametrize("query", doc_queries(), ids=lambda q: norm(q)[:48])
def test_every_documented_query_exists_in_the_code(query, blob):
    """Each documented query must appear verbatim in tools.py or signature.py."""
    needle = norm(strip_comments(query))

    # An f-string query in the source writes its literal braces doubled (`{{name: ...}}`), so a
    # doc block copied from the rendered query will not match the source text unless we account
    # for that. Accept either spelling.
    def present(fragment: str) -> bool:
        doubled = fragment.replace("{", "{{").replace("}", "}}")
        return fragment in blob or doubled in blob

    if PLACEHOLDER in needle:
        # Check the fixed halves either side of the interpolated depth.
        for part in (p.strip() for p in needle.split(PLACEHOLDER)):
            if len(part) > 12:
                assert present(part), f"documented fragment not found in code:\n{part[:160]}"
        return

    assert present(needle), f"documented query not found in code:\n{needle[:220]}"


def test_no_documented_query_uses_a_rejected_form():
    """Forms FalkorDB rejects must not reappear in a code block as if they worked.

    Only the code blocks are scanned: the prose deliberately quotes these forms when explaining
    why they do not work.
    """
    blocks = norm(" ".join(doc_queries()))
    for banned, why in (
        ("WHERE NOT EXISTS((", "exists() rejects traversal patterns"),
        ("*1..$hops", "variable-length bounds cannot be parameterised"),
    ):
        assert banned not in blocks, f"{banned} — {why}"

    for query in doc_queries():
        n = norm(query)
        if "DEPENDS_ON*" in n and "ALL(" in n:
            assert "relationships(" in n, \
                "a variable-length alias is a Path; ALL(...) needs relationships(...)"


def test_algorithms_claimed_in_docs_are_actually_called():
    """Every `algo.*` the docs present as in use must appear in the source tree.

    This is the check that caught `algo.SPpaths` being claimed in the README and the submission
    while existing nowhere in the code.
    """
    code = "\n".join(p.read_text() for p in (ROOT / "ariadne").glob("*.py"))
    called = set(re.findall(r"algo\.(\w+)", code))

    table = DOC.read_text().split("## Graph algorithms, and where each one actually runs")[1]
    table = table.split("## Designed, not implemented")[0]
    claimed = set(re.findall(r"`algo\.(\w+)`", table))

    missing = claimed - called
    assert not missing, f"docs claim algorithms the code never calls: {sorted(missing)}"


def test_unimplemented_things_are_declared_as_such():
    """The doc must keep naming what is designed but absent, so a reader is not misled."""
    section = DOC.read_text().split("## Designed, not implemented")[-1]
    code = "\n".join(p.read_text() for p in (ROOT / "ariadne").glob("*.py"))

    if "algo.SPpaths" in section:
        assert "algo.SPpaths" not in code, \
            "SPpaths is implemented now — move it out of 'Designed, not implemented'"
    if "queryNodes" in code.split("def recall_plan")[-1].split("def ")[0]:
        pytest.fail("recall_plan uses vector recall now — update the doc section")
