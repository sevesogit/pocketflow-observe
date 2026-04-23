"""
pocketflow_observe.logging — @log_flow decorator.

Adds pretty console logging (Rich) to every node execution inside a Flow.
Works on vanilla `pocketflow.Flow` subclasses — no import of pocketflow-observe
needed in user node code.

Usage:

    from pocketflow import Flow
    from pocketflow_observe import log_flow

    @log_flow()
    class MyFlow(Flow):
        pass

    # or with options
    @log_flow(flow_name="DataPipeline", show_types=True)
    class MyFlow(Flow):
        pass
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Type

from ._core import node_observation_type
from ._logger import get_logger

_log = get_logger("pocketflow_observe")


def log_flow(
    flow_name: Optional[str] = None,
    *,
    show_types: bool = True,
    capture_phases: bool = True,
) -> Callable[[Type], Type]:
    """Decorator that adds console logging to every node run in a Flow.

    Args:
        flow_name: Override the displayed flow name (defaults to class name).
        show_types: If True, show `(agent)` / `(tool)` / etc. badges next to
            node names when the node declares an `observation_type`.
        capture_phases: If True, log each node's prep/exec/post at DEBUG level.
            Turn off for noise reduction in long flows.
    """

    def decorator(cls: Type) -> Type:
        getattr(cls, "_orch", None) or getattr(cls, "_orch_async", None)
        hasattr(cls, "_orch_async") and not hasattr(cls, "_orch")

        display_name = flow_name or cls.__name__

        # -- wrap each node's _run / _run_async once the flow starts --
        #
        # We do this at flow-start-time (not decorator-time) because nodes
        # are typically instantiated inside __init__ or passed in as `start`,
        # and we want the wrapping to follow successors dynamically.

        def _wrap_node(node: Any, is_async_node: bool = False) -> None:
            if getattr(node, "__pocketflow_observe_log_wrapped__", False):
                return
            node.__pocketflow_observe_log_wrapped__ = True

            if is_async_node:
                original = node._run_async

                async def wrapped(shared: Dict[str, Any]) -> Any:
                    return await _run_with_logging(
                        node, shared, original, show_types, capture_phases, is_async=True
                    )

                node._run_async = wrapped
            else:
                original = node._run

                def wrapped(shared: Dict[str, Any]) -> Any:
                    return _run_with_logging_sync(
                        node, shared, original, show_types, capture_phases
                    )

                node._run = wrapped

        def _walk_and_wrap(start_node: Any, is_async_flow: bool) -> None:
            """BFS through the node graph, wrapping _run on each."""
            seen = set()
            stack = [start_node]
            while stack:
                n = stack.pop()
                if n is None or id(n) in seen:
                    continue
                seen.add(id(n))
                # Use async wrapper if the node has _run_async
                _wrap_node(n, is_async_node=hasattr(n, "_run_async"))
                for succ in getattr(n, "successors", {}).values():
                    stack.append(succ)

        # -- wrap the flow-level run / run_async --

        if hasattr(cls, "run_async"):
            original_flow_run = cls.run_async

            async def new_run_async(self: Any, shared: Dict[str, Any]) -> Any:
                _walk_and_wrap(self.start_node, is_async_flow=True)
                _log.flow_start(display_name)
                t0 = time.perf_counter()
                try:
                    result = await original_flow_run(self, shared)
                    _log.flow_end(display_name, time.perf_counter() - t0)
                    return result
                except Exception as e:
                    _log.flow_error(display_name, e)
                    raise

            cls.run_async = new_run_async  # type: ignore[method-assign]

        if hasattr(cls, "run"):
            original_flow_run_sync = cls.run

            def new_run(self: Any, shared: Dict[str, Any]) -> Any:
                _walk_and_wrap(self.start_node, is_async_flow=False)
                _log.flow_start(display_name)
                t0 = time.perf_counter()
                try:
                    result = original_flow_run_sync(self, shared)
                    _log.flow_end(display_name, time.perf_counter() - t0)
                    return result
                except Exception as e:
                    _log.flow_error(display_name, e)
                    raise

            cls.run = new_run  # type: ignore[method-assign]

        return cls

    return decorator


# ---------------------------------------------------------------------------
# per-node run wrappers
# ---------------------------------------------------------------------------


def _run_with_logging_sync(
    node: Any,
    shared: Dict[str, Any],
    original: Callable,
    show_types: bool,
    capture_phases: bool,
) -> Any:
    name = type(node).__name__
    obs_type = node_observation_type(node) if show_types else "span"
    _log.node_start(name, obs_type)
    t0 = time.perf_counter()

    if capture_phases:
        # Monkey-patch prep/exec/post briefly so we can log their outputs.
        # We restore them right after, so nothing leaks across runs.
        _wrap_phases_for_logging(node, name)
    try:
        action = original(shared)
        _log.node_end(name, action, time.perf_counter() - t0)
        return action
    except Exception as e:
        _log.node_error(name, e)
        raise
    finally:
        if capture_phases:
            _unwrap_phases(node)


async def _run_with_logging(
    node: Any,
    shared: Dict[str, Any],
    original: Callable,
    show_types: bool,
    capture_phases: bool,
    is_async: bool,
) -> Any:
    name = type(node).__name__
    obs_type = node_observation_type(node) if show_types else "span"
    _log.node_start(name, obs_type)
    t0 = time.perf_counter()
    if capture_phases:
        _wrap_phases_for_logging(node, name, is_async=is_async)
    try:
        action = await original(shared)
        _log.node_end(name, action, time.perf_counter() - t0)
        return action
    except Exception as e:
        _log.node_error(name, e)
        raise
    finally:
        if capture_phases:
            _unwrap_phases(node)


# ---------------------------------------------------------------------------
# phase-level debug logging (opt-in via capture_phases=True)
# ---------------------------------------------------------------------------


def _wrap_phases_for_logging(node: Any, name: str, is_async: bool = False) -> None:
    """Transiently wrap prep/exec/post so their return values hit DEBUG logs."""

    for method_name in ("prep", "exec", "post"):
        if is_async:
            method_name = method_name + "_async"
        original = getattr(node, method_name, None)
        if original is None:
            continue
        setattr(node, f"__pocketflow_observe_orig_{method_name}", original)

        def make_wrapper(orig, phase):
            if is_async:

                async def w(*args, **kwargs):
                    result = await orig(*args, **kwargs)
                    _log.node_phase(name, phase, result)
                    return result

                return w

            def w(*args, **kwargs):
                result = orig(*args, **kwargs)
                _log.node_phase(name, phase, result)
                return result

            return w

        setattr(node, method_name, make_wrapper(original, method_name))


def _unwrap_phases(node: Any) -> None:
    for method_name in ("prep", "exec", "post", "prep_async", "exec_async", "post_async"):
        attr = f"__pocketflow_observe_orig_{method_name}"
        if hasattr(node, attr):
            setattr(node, method_name, getattr(node, attr))
            delattr(node, attr)


# ---------------------------------------------------------------------------
# log_node — observe a single node, without needing a Flow
# ---------------------------------------------------------------------------


def log_node(
    target: Any = None,
    *,
    show_types: bool = True,
    capture_phases: bool = True,
) -> Any:
    """Add console logging to a single node, class or instance.

    Three call shapes, all supported:

        # 1. As a class decorator (no args)
        @log_node
        class MyNode(Node): ...

        # 2. As a class decorator with args
        @log_node(capture_phases=False)
        class MyNode(Node): ...

        # 3. On an instance at runtime
        node = SomeNode()
        node = log_node(node)
        node.run(shared)

    Calls `.run(shared)` on the node exactly like vanilla pocketflow —
    you just get logs around it. Unlike `@log_flow`, this doesn't walk
    successors; if the node has successors and you call `.run()` on it,
    pocketflow warns you (that's pocketflow's own behaviour, unchanged).
    """

    def _apply(obj: Any) -> Any:
        # If it's a class, patch its _run / _run_async methods.
        if isinstance(obj, type):
            return _wrap_node_class(obj, show_types, capture_phases)
        # Otherwise treat as an instance — patch the bound method on that
        # single object only, not the class.
        _wrap_node_instance(obj, show_types, capture_phases)
        return obj

    # Zero-arg decorator form: @log_node (not @log_node())
    if target is not None and (isinstance(target, type) or _looks_like_node_instance(target)):
        return _apply(target)

    # With-args form: @log_node(show_types=False) — return the real decorator
    def decorator(obj: Any) -> Any:
        return _apply(obj)

    return decorator


def _looks_like_node_instance(obj: Any) -> bool:
    """Heuristic: does this object look like a pocketflow Node instance?"""
    return not isinstance(obj, type) and (hasattr(obj, "_run") or hasattr(obj, "_run_async"))


def _wrap_node_class(cls: Type, show_types: bool, capture_phases: bool) -> Type:
    """Patch _run / _run_async on the class so every instance is logged."""
    is_async = hasattr(cls, "_run_async") and not hasattr(cls, "_run")

    if is_async or hasattr(cls, "_run_async"):
        original = cls._run_async  # type: ignore[attr-defined]

        async def wrapped(self: Any, shared: Dict[str, Any]) -> Any:
            bound = lambda s: original(self, s)  # noqa: E731
            return await _run_with_logging(
                self, shared, bound, show_types, capture_phases, is_async=True
            )

        cls._run_async = wrapped  # type: ignore[attr-defined]

    if hasattr(cls, "_run"):
        original_sync = cls._run  # type: ignore[attr-defined]

        def wrapped_sync(self: Any, shared: Dict[str, Any]) -> Any:
            bound = lambda s: original_sync(self, s)  # noqa: E731
            return _run_with_logging_sync(self, shared, bound, show_types, capture_phases)

        cls._run = wrapped_sync  # type: ignore[attr-defined]

    return cls


def _wrap_node_instance(node: Any, show_types: bool, capture_phases: bool) -> None:
    """Patch _run / _run_async on this one instance (leaves the class alone)."""
    if getattr(node, "__pocketflow_observe_log_wrapped__", False):
        return
    node.__pocketflow_observe_log_wrapped__ = True

    if hasattr(node, "_run_async"):
        original = node._run_async

        async def wrapped(shared: Dict[str, Any]) -> Any:
            return await _run_with_logging(
                node, shared, original, show_types, capture_phases, is_async=True
            )

        node._run_async = wrapped

    if hasattr(node, "_run"):
        original_sync = node._run

        def wrapped_sync(shared: Dict[str, Any]) -> Any:
            return _run_with_logging_sync(node, shared, original_sync, show_types, capture_phases)

        node._run = wrapped_sync
