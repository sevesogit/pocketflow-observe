"""
pocketflow_observe.core — small helpers shared by @log_flow and @trace_flow decorators.

Design notes:

* We do NOT subclass pocketflow.Flow or Node. The decorators wrap whatever
  the user passes in, including vanilla PocketFlow.

* Per-node observation types: nodes can declare `observation_type` as a
  class attribute (no import of anything from pocketflow_observe required — it's
  just a string). The tracing decorator reads it; the logging decorator
  ignores it.

* Resolver pattern: decorator args that should be computed per-run (e.g.
  session_id depending on `shared`) can be passed as callables. A callable
  of one arg receives the shared store; a callable of zero args is called
  bare. String/int/None values are returned as-is.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, Optional, Union

# A flow-level param can be a literal, or a callable taking `shared`.
Resolvable = Union[Any, Callable[..., Any]]


def resolve(value: Resolvable, shared: Dict[str, Any]) -> Any:
    """Evaluate a Resolvable against the current `shared` dict.

    - If `value` is callable and accepts one arg, call it with `shared`.
    - If it's callable with zero args, call it bare.
    - Otherwise return as-is.
    """
    if not callable(value):
        return value
    try:
        sig = inspect.signature(value)
        if len(sig.parameters) >= 1:
            return value(shared)
        return value()
    except (ValueError, TypeError):
        # Built-ins without signatures, or misbehaving callables — best effort
        try:
            return value(shared)
        except TypeError:
            return value()


def node_observation_type(
    node: Any,
    overrides: Optional[Dict[str, str]] = None,
    default: str = "span",
) -> str:
    """Pick the Langfuse observation type for a node.

    Priority:
      1. Explicit override in the decorator's `node_types` dict
         (keyed by class name)
      2. `observation_type` class attribute on the node
      3. `default`
    """
    name = type(node).__name__
    if overrides and name in overrides:
        return overrides[name]
    return getattr(node, "observation_type", default)


def safe_repr(value: Any, max_len: int = 2000) -> str:
    """Bounded repr for logging / span payloads — never raises."""
    try:
        s = repr(value)
    except Exception:
        return f"<unreprable {type(value).__name__}>"
    if len(s) > max_len:
        return s[:max_len] + f"… (+{len(s) - max_len})"
    return s
