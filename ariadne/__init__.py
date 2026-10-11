"""Ariadne — an on-call agent that reasons over, and remembers into, a FalkorDB graph."""

from . import env as _env

__version__ = "0.1.0"

# Load .env on import so every entry point (agent, replay, mcp_server, pytest) sees the same
# configuration. Existing environment variables take precedence, and nothing is printed.
_env.load()
