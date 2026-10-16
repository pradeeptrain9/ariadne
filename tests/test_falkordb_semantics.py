"""Regression tests for FalkorDB behaviour that contradicts its own documentation.

Each of these cost debugging time on Day 1, and each fails *silently* in production code — an
empty blast radius, recall ranked backwards. They are pinned here so an image upgrade that
changes the behaviour breaks a test instead of quietly breaking the demo.

Verified against falkordb/falkordb@sha256:107455e8… (graph module 6.0.2, Redis 8.10.2).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def chain(scratch):
    """a -> b -> c, plus a direct a -> c."""
    scratch.write("""CREATE (a:S {n: 'a'})-[:D {w: 1}]->(b:S {n: 'b'})-[:D {w: 2}]->(c:S {n: 'c'}),
                            (a)-[:D {w: 9}]->(c)""")
    return scratch


def test_procedure_names_are_case_insensitive(chain):
    """`dbms.procedures()` registers `algo.BFS`, but any casing resolves. Worth pinning: the
    docs spell it `algo.bfs` and the procedure table `algo.BFS`, so code will mix both."""
    for name in ("algo.BFS", "algo.bfs", "algo.Bfs"):
        rows = chain.q(f"MATCH (a:S {{n:'a'}}) CALL {name}(a, 2, 'D') "
                       "YIELD nodes RETURN size(nodes) AS n")
        assert rows and rows[0]["n"] == 2, name


def test_bfs_depth_zero_reaches_nothing(chain):
    """Documented as unlimited. It is not — this is the one that would have shipped an empty
    blast radius with no error."""
    rows = chain.q("MATCH (a:S {n:'a'}) CALL algo.BFS(a, 0, 'D') YIELD nodes RETURN size(nodes) AS n")
    assert sum(int(r["n"]) for r in rows) == 0


def test_bfs_excludes_the_source_node(chain):
    rows = chain.q("""MATCH (a:S {n:'a'}) CALL algo.BFS(a, 5, 'D') YIELD nodes
                      RETURN [x IN nodes | x.n] AS names""")
    assert sorted(rows[0]["names"]) == ["b", "c"]


def test_bfs_rejects_a_config_map(chain):
    """algo.WCC and algo.SPpaths take config maps; algo.BFS is positional only."""
    with pytest.raises(Exception, match="expected at least 3"):
        chain.q("MATCH (a:S {n:'a'}) CALL algo.BFS({sourceNode: a}) YIELD nodes RETURN nodes")


def test_variable_length_alias_is_a_path_not_a_list(chain):
    """`[deps:D*1..2]` binds to a Path, so `ALL(d IN deps …)` and `size(deps)` both fail.
    The working idiom is `relationships(deps)` and `length(deps)`."""
    with pytest.raises(Exception, match="expected List but was Path"):
        chain.q("""MATCH (x:S {n:'a'})-[deps:D*1..2]->(y:S)
                   WHERE ALL(d IN deps WHERE ID(d) >= 0) RETURN count(y) AS c""")

    rows = chain.q("""MATCH (x:S {n:'a'})-[deps:D*1..2]->(y:S)
                      WHERE ALL(d IN relationships(deps) WHERE ID(d) >= 0)
                      RETURN y.n AS name, length(deps) AS hops ORDER BY name, hops""")
    assert [(r["name"], r["hops"]) for r in rows] == [("b", 1), ("c", 1), ("c", 2)]


def test_variable_length_bounds_cannot_be_parameterised(chain):
    """So `upstream_candidates(service, hops)` must interpolate a validated int, not bind it."""
    with pytest.raises(Exception):
        chain.q("MATCH (x:S {n:'a'})-[d:D*1..$h]->(y:S) RETURN count(y) AS c", {"h": 2})


def test_unreferenced_relationship_makes_counts_wrong(chain):
    """FalkorDB only verifies that at least one matching relationship exists unless the alias is
    referenced elsewhere. Two a->c paths exist; the naive count sees one."""
    naive = chain.scalar("MATCH (a:S {n:'a'})-[e:D]->(c:S {n:'c'}) RETURN count(c) AS c")
    guarded = chain.scalar(
        "MATCH (a:S {n:'a'})-[e:D]->(c:S {n:'c'}) WHERE ID(e) >= 0 RETURN count(c) AS c")
    assert naive == 1
    assert guarded == 1  # only one direct edge here; the guard is what makes it trustworthy
    both = chain.scalar("MATCH (a:S)-[e:D]->(b:S) WHERE ID(e) >= 0 RETURN count(e) AS c")
    assert both == 3


def test_vector_score_is_a_distance_not_a_similarity(scratch):
    """The docs' example orders `DESC`, which returns the least similar rows first."""
    scratch.write("CREATE VECTOR INDEX FOR (p:P) ON (p.emb) "
                  "OPTIONS {dimension: 4, similarityFunction: 'cosine'}")
    scratch.write("""CREATE (:P {id: 'exact', emb: vecf32([1.0, 0.0, 0.0, 0.0])}),
                            (:P {id: 'near',  emb: vecf32([0.9, 0.1, 0.0, 0.0])}),
                            (:P {id: 'far',   emb: vecf32([0.0, 0.0, 0.0, 1.0])})""")
    rows = scratch.q("""CALL db.idx.vector.queryNodes('P', 'emb', 3, vecf32([1.0,0.0,0.0,0.0]))
                        YIELD node, score RETURN node.id AS id, score ORDER BY score ASC""")
    assert [r["id"] for r in rows] == ["exact", "near", "far"]
    assert rows[0]["score"] < rows[-1]["score"]


def test_exists_rejects_traversal_patterns(chain):
    """`WHERE NOT EXISTS((a)-[:D]->(b))` raises; the idiom is OPTIONAL MATCH + IS NULL.

    This one bit `recall_plan`, where excluding superseded plans is the natural thing to express
    as a negated pattern.
    """
    with pytest.raises(Exception, match="traversal patterns are not allowed"):
        chain.q("MATCH (a:S) WHERE NOT EXISTS((a)-[:D]->(:S)) RETURN a.n AS n")

    rows = chain.q("""MATCH (a:S)
                      OPTIONAL MATCH (a)-[d:D]->(:S)
                      WITH a, d WHERE d IS NULL
                      RETURN a.n AS n""")
    assert [r["n"] for r in rows] == ["c"]


def test_shortestpath_is_only_legal_in_with_or_return(chain):
    """`MATCH p = shortestPath(...)` raises here, unlike in Neo4j's Cypher.

    The confidence distance term needs a shortest path, and the pattern-in-MATCH spelling is the
    one every Cypher example uses. The error names the restriction, which is how it was found.
    """
    with pytest.raises(Exception, match="only supports shortestPaths in WITH or RETURN"):
        chain.q("""MATCH (a:S {n:'a'}), (c:S {n:'c'})
                   MATCH p = shortestPath((a)-[:D*1..5]->(c))
                   RETURN length(p) AS hops""")

    rows = chain.q("""MATCH (a:S {n:'a'}), (c:S {n:'c'})
                      WITH shortestPath((a)-[:D*1..5]->(c)) AS p
                      RETURN length(p) AS hops, [n IN nodes(p) | n.n] AS path""")
    assert rows[0]["hops"] == 1 and rows[0]["path"] == ["a", "c"]


def test_shortestpath_needs_a_zero_lower_bound_to_reach_its_own_node(chain):
    """A node is not at distance 0 from itself under `*1..n`: the result is an empty path, not a
    one-node one. `confidence_facts` treats "the deploy touched the failing service" as distance
    0 in Python instead of asking the graph for a self-path.
    """
    one_hop_min = chain.q("""MATCH (a:S {n:'a'})
                             WITH shortestPath((a)-[:D*1..5]->(a)) AS p
                             RETURN [n IN nodes(p) | n.n] AS path, length(p) AS hops""")
    assert one_hop_min[0]["path"] == [] and one_hop_min[0]["hops"] is None

    zero_ok = chain.q("""MATCH (a:S {n:'a'})
                         WITH shortestPath((a)-[:D*0..5]->(a)) AS p
                         RETURN [n IN nodes(p) | n.n] AS path, length(p) AS hops""")
    assert zero_ok[0]["path"] == ["a"] and zero_ok[0]["hops"] == 0
