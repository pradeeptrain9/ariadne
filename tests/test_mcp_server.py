"""The MCP surface must be the *same* tools, not a second copy that drifts."""

from __future__ import annotations

import asyncio

import pytest

from ariadne import mcp_server, tools as T


@pytest.fixture
def server():
    return mcp_server.build(allow_writes=False)


@pytest.fixture
def writable():
    return mcp_server.build(allow_writes=True)


def _listed(server):
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_read_only_by_default(server):
    """An external client holding a stdio pipe should not be able to write plans into the
    memory graph unless the operator opted in."""
    names = set(_listed(server))
    assert names == set(T.READ_ONLY)
    assert "record_outcome" not in names


def test_writes_can_be_enabled(writable):
    assert "record_outcome" in _listed(writable)


def _prose(description: str) -> str:
    """The guidance text, normalised.

    The two SDKs format the same docstring differently: the Anthropic SDK dedents it and moves
    the `Args:` block into the schema, while MCP passes the raw docstring through. Neither is
    wrong, so the comparison is on the prose both of them carry.
    """
    head = description.split("Args:")[0]
    return " ".join(head.split())


def test_schemas_match_the_anthropic_surface(writable):
    """Both transports derive from the same functions, so the guidance and the parameters must
    agree. If this fails, the two surfaces have drifted and the MCP tools are a stale copy."""
    listed = _listed(writable)
    assert set(listed) == set(T.TOOL_FUNCS)

    for tool in T.TOOLS:
        d = tool.to_dict() if hasattr(tool, "to_dict") else dict(tool)
        mcp_tool = listed[d["name"]]
        assert _prose(mcp_tool.description) == _prose(d["description"]), d["name"]

        mine = mcp_tool.input_schema
        theirs = d["input_schema"]
        assert set(mine["properties"]) == set(theirs["properties"]), d["name"]
        assert set(mine.get("required", [])) == set(theirs.get("required", [])), d["name"]
        for prop, spec in theirs["properties"].items():
            assert mine["properties"][prop].get("type") == spec.get("type"), f"{d['name']}.{prop}"


def test_descriptions_come_from_the_docstrings(server):
    listed = _listed(server)
    assert "CALL THIS FIRST" in listed["recall_plan"].description
    assert "overlap" in listed["suspect_commits"].description


def test_annotations_mark_reads_as_read_only(writable):
    listed = _listed(writable)
    assert listed["owner_of"].annotations.read_only_hint is True
    assert listed["record_outcome"].annotations.read_only_hint is False
    assert listed["record_outcome"].annotations.destructive_hint is False


def test_tools_execute_through_mcp_against_the_graph(seeded, monkeypatch, server):
    """Call a tool the way a client would: the wrapper has to bind the graph itself, since the
    graph is deliberately absent from the schema."""
    monkeypatch.setattr(mcp_server, "_graph", seeded)
    result = asyncio.run(server.call_tool("blast_radius", {"service": "orders-db"}))
    payload = result[1] if isinstance(result, tuple) else result
    assert payload
