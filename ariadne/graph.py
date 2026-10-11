"""FalkorDB connection and query helpers.

Every query in the project goes through `Ariadne.q`, which returns a list of dicts keyed by the
query's RETURN aliases. Returning dicts rather than positional tuples keeps the tool layer
readable and means a reordered RETURN clause cannot silently shift a column.
"""

from __future__ import annotations

import os
from typing import Any

from falkordb import FalkorDB

GRAPH_NAME = os.environ.get("ARIADNE_GRAPH", "ariadne")
HOST = os.environ.get("FALKORDB_HOST", "localhost")
PORT = int(os.environ.get("FALKORDB_PORT", "6379"))


class Ariadne:
    """Thin wrapper over a single FalkorDB graph."""

    def __init__(self, name: str = GRAPH_NAME, host: str = HOST, port: int = PORT) -> None:
        self._db = FalkorDB(host=host, port=port)
        self.name = name
        self.g = self._db.select_graph(name)

    # --- queries -------------------------------------------------------------

    def q(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run a query and return rows as dicts keyed by RETURN alias."""
        res = self.g.query(cypher, params or {})
        header = [h[1] if isinstance(h, (list, tuple)) else h for h in (res.header or [])]
        return [dict(zip(header, row)) for row in (res.result_set or [])]

    def one(self, cypher: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        rows = self.q(cypher, params)
        return rows[0] if rows else None

    def scalar(self, cypher: str, params: dict[str, Any] | None = None) -> Any:
        row = self.one(cypher, params)
        return next(iter(row.values())) if row else None

    def write(self, cypher: str, params: dict[str, Any] | None = None) -> dict[str, int]:
        """Run a writing query, returning the statistics FalkorDB reports."""
        res = self.g.query(cypher, params or {})
        return {
            "nodes_created": res.nodes_created or 0,
            "relationships_created": res.relationships_created or 0,
            "properties_set": res.properties_set or 0,
            "nodes_deleted": res.nodes_deleted or 0,
        }

    # --- lifecycle -----------------------------------------------------------

    def drop(self) -> None:
        """Delete the whole graph. Used by the seeder and the tests, never in the agent path."""
        try:
            self.g.delete()
        except Exception:
            pass  # graph did not exist
        self.g = self._db.select_graph(self.name)

    def exists(self) -> bool:
        return self.name in self._db.list_graphs()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Ariadne":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
