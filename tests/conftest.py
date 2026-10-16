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


def _connect(name: str) -> Ariadne:
    """Open a graph, or skip the test if there is no FalkorDB to open it on.

    The connection is made *here*, inside the guard: `Ariadne.__init__` builds the client and the
    driver dials the socket eagerly, so constructing it outside a try raised
    `redis.exceptions.ConnectionError` before any skip could run — turning "no FalkorDB" into a
    wall of red errors instead of skips.
    """
    try:
        a = Ariadne(name)
        a.g.query("RETURN 1")
    except Exception as exc:  # pragma: no cover - environment problem, not a test failure
        pytest.skip(f"FalkorDB unreachable: {exc}")
    return a


@pytest.fixture(scope="session")
def seeded() -> Ariadne:
    a = _connect("ariadne_test")
    seed_mod.seed(a)
    yield a
    a.drop()
    a.close()


@pytest.fixture
def scratch() -> Ariadne:
    a = _connect("ariadne_scratch")
    a.drop()
    yield a
    a.drop()
    a.close()
