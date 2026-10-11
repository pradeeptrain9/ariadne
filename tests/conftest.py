"""Shared fixtures.

The suite talks to a real FalkorDB — these are not unit tests with a mocked driver. Half the
bugs this project can have are FalkorDB semantics (a variable-length alias is a Path, not a
list; `algo.BFS` at depth 0 reaches nothing), and a mock would assert our misunderstanding
rather than the server's behaviour.

The seeded graph is built once per session into a separate graph name, so running the tests
never touches the demo graph.
"""

from __future__ import annotations

import pytest

from ariadne.graph import Ariadne
from ariadne import seed as seed_mod


@pytest.fixture(scope="session")
def seeded() -> Ariadne:
    a = Ariadne("ariadne_test")
    try:
        a.g.query("RETURN 1")
    except Exception as exc:  # pragma: no cover - environment problem, not a test failure
        pytest.skip(f"FalkorDB unreachable: {exc}")
    seed_mod.seed(a)
    yield a
    a.drop()
    a.close()


@pytest.fixture
def scratch() -> Ariadne:
    a = Ariadne("ariadne_scratch")
    a.drop()
    yield a
    a.drop()
    a.close()
