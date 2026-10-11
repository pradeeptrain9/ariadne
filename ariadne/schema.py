"""Indexes, constraints and the vector index.

Idempotent: every statement is tolerant of already existing, so this can run on every boot.

FalkorDB requires a range index on a property before a unique constraint can be created over it,
so the order here matters.
"""

from __future__ import annotations

from .graph import Ariadne

# label -> property that uniquely identifies a node of that label
UNIQUE: dict[str, str] = {
    "Service": "name",
    "Deploy": "id",
    "Commit": "sha",
    "Person": "handle",
    "Alert": "id",
    "Incident": "id",
    "FailureMode": "name",
    "Runbook": "id",
    "Plan": "id",
    "Tool": "name",
    "Condition": "name",
}

# extra range indexes on properties we filter or sort by
RANGE: list[tuple[str, str]] = [
    ("Deploy", "at"),
    ("Alert", "at"),
    ("Incident", "at"),
    ("Incident", "signature"),
    ("Plan", "signature"),
    ("Service", "tier"),
]

EMBED_DIM = 384
EMBED_SIMILARITY = "cosine"


def _ignore_exists(fn, *args, **kwargs) -> bool:
    """Run a schema call, swallowing 'already indexed / already exists' errors."""
    try:
        fn(*args, **kwargs)
        return True
    except Exception as exc:  # falkordb raises a generic ResponseError
        msg = str(exc).lower()
        if "already" in msg or "exist" in msg:
            return False
        raise


def apply(a: Ariadne, *, with_vector: bool = True) -> dict[str, int]:
    """Create every index and constraint. Returns counts of what was newly created."""
    created = {"range": 0, "unique": 0, "vector": 0}

    for label, prop in UNIQUE.items():
        if _ignore_exists(a.g.create_node_range_index, label, prop):
            created["range"] += 1

    for label, prop in RANGE:
        if _ignore_exists(a.g.create_node_range_index, label, prop):
            created["range"] += 1

    # Unique constraints need the range index above to exist first.
    for label, prop in UNIQUE.items():
        if _ignore_exists(a.g.create_node_unique_constraint, label, prop):
            created["unique"] += 1

    if with_vector:
        if _ignore_exists(
            a.g.create_node_vector_index,
            "Plan",
            "embedding",
            dim=EMBED_DIM,
            similarity_function=EMBED_SIMILARITY,
        ):
            created["vector"] += 1

    return created


def describe(a: Ariadne) -> dict[str, list]:
    """What the server actually has. Used by `verify` and when explaining the schema."""
    return {
        "indices": [list(r.values()) for r in a.q("CALL db.indexes()")],
        "constraints": [list(r.values()) for r in a.q("CALL db.constraints()")],
    }


if __name__ == "__main__":
    with Ariadne() as a:
        print("created:", apply(a))
        d = describe(a)
        print(f"indices: {len(d['indices'])}  constraints: {len(d['constraints'])}")
