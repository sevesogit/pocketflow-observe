"""
pocketflow_observe.tracing — @trace_flow decorator.

Adds Langfuse tracing to a PocketFlow Flow class. Works on vanilla
pocketflow.Flow — no subclassing of pocketflow-observe types required.

Key features:
  * Per-node `observation_type` — from the node class, or override in the
    decorator via `node_types={"ClassName": "tool"}`
  * Flow-level trace attributes: session_id, user_id, tags, metadata,
    version, release — all can be literals OR callables taking `shared`
  * Per-node input/output/metadata/usage/cost from the node's return value
    (when it returns a dict with reserved keys), OR via the exec_post_hook
    argument
  * Graceful no-op when Langfuse is not configured — flow still runs normally
  * Stackable with @log_flow in either order

Usage:

    from pocketflow import Flow
    from pocketflow_observe import trace_flow

    @trace_flow(
        flow_name="ResearchAgent",
        session_id=lambda shared: shared["conversation_id"],
        user_id="user_123",
        tags=["prod", "v2"],
        metadata={"region": "eu"},
        node_types={"SearchDB": "retriever", "CallLLM": "generation"},
    )
    class ResearchFlow(Flow):
        pass
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, Optional, Type

from ._core import Resolvable, node_observation_type, resolve, safe_repr

_warn_log = logging.getLogger("pocketflow_observe.tracing")

# ---------------------------------------------------------------------------
# Langfuse detection — soft dependency.
# ---------------------------------------------------------------------------


def _init_langfuse() -> tuple:
    """Detect Langfuse availability and return (enabled, client).

    Emits warnings when configuration is incomplete or broken.
    Returns (False, None) for every non-happy path.
    """
    if os.getenv("POCKETFLOW_OBSERVE_ENABLED", "1") == "0":
        return False, None

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")

    try:
        from langfuse import get_client  # type: ignore
        has_langfuse = True
    except ImportError:
        has_langfuse = False

    if not has_langfuse:
        _warn_log.warning(
            "langfuse is not installed — @trace_flow / @trace_node will be no-ops. "
            "Install it with: pip install 'pocketflow-observe[tracing]'"
        )
        return False, None

    if not public_key and not secret_key:
        _warn_log.warning(
            "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set — "
            "tracing is disabled. Set both env vars to enable Langfuse tracing."
        )
        return False, None

    if not public_key:
        _warn_log.warning(
            "LANGFUSE_PUBLIC_KEY is not set — tracing is disabled. "
            "Both LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required."
        )
        return False, None

    if not secret_key:
        _warn_log.warning(
            "LANGFUSE_SECRET_KEY is not set — tracing is disabled. "
            "Both LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required."
        )
        return False, None

    try:
        client = get_client()
        return True, client
    except Exception as exc:
        _warn_log.warning(
            "Failed to initialize Langfuse client — tracing is disabled. "
            "Check your LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, and LANGFUSE_HOST. "
            "Error: %s: %s",
            type(exc).__name__,
            exc,
        )
        return False, None


_ENABLED, _langfuse = _init_langfuse()


def is_enabled() -> bool:
    return _ENABLED


def flush() -> None:
    """Flush pending spans. Call at program end / in short-lived processes."""
    if _ENABLED and _langfuse is not None:
        _langfuse.flush()


# ---------------------------------------------------------------------------
# trace_llm — nested-generation helper
# ---------------------------------------------------------------------------

def trace_llm(
    name: str,
    model: str,
    input: Any,
    output: Any,
    usage_details: Optional[Dict[str, int]] = None,
    cost_details: Optional[Dict[str, float]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    as_type: str = "generation",
    model_parameters: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a one-shot LLM generation under the currently active span.

    Safe no-op when tracing is disabled. Typical use: call from inside a
    node's `exec` after you've called the LLM, to create a nested
    `generation` span with token usage and cost.
    """
    if not _ENABLED or _langfuse is None:
        return

    start_kwargs: Dict[str, Any] = {"as_type": as_type, "name": name,
                                    "model": model, "input": input}
    if model_parameters:
        start_kwargs["model_parameters"] = model_parameters

    with _langfuse.start_as_current_observation(**start_kwargs) as gen:
        update: Dict[str, Any] = {"output": output}
        if usage_details:
            update["usage_details"] = usage_details
        if cost_details:
            update["cost_details"] = cost_details
        if metadata:
            update["metadata"] = metadata
        gen.update(**update)


# ---------------------------------------------------------------------------
# Per-node hook type — users can customise what gets attached to each span
# ---------------------------------------------------------------------------

# Signature: fn(node, shared, action, elapsed) -> dict of span.update kwargs
# Return dict may contain: input, output, metadata, usage_details,
# cost_details, model, model_parameters, level, status_message
NodeHook = Callable[[Any, Dict[str, Any], Optional[str], float], Dict[str, Any]]


# ---------------------------------------------------------------------------
# The decorator
# ---------------------------------------------------------------------------

def trace_flow(
    flow_name: Optional[str] = None,
    *,
    # Flow-level trace attributes (Langfuse "trace" scope)
    session_id: Resolvable = None,
    user_id: Resolvable = None,
    tags: Resolvable = None,  # list[str] or callable
    release: Resolvable = None,
    version: Resolvable = None,
    metadata: Resolvable = None,  # dict or callable

    # Flow-level span attributes
    flow_type: str = "chain",

    # Per-node observation-type overrides: {"ClassName": "tool"}
    node_types: Optional[Dict[str, str]] = None,

    # Per-node hook — lets users enrich every node's span
    node_hook: Optional[NodeHook] = None,

    # IO capture — turn off if shared contains secrets or huge payloads
    capture_input: bool = True,
    capture_output: bool = True,
    max_payload_len: int = 2000,
) -> Callable[[Type], Type]:
    """Decorator: add Langfuse tracing to a Flow class.

    Zero-arg form works for most cases:

        @trace_flow()
        class MyFlow(Flow): pass

    All resolvable args (session_id, user_id, tags, release, version,
    metadata) accept either a literal or a callable taking `shared`. This
    lets you bind dynamic values per run:

        session_id=lambda shared: shared["conversation_id"]
    """

    def decorator(cls: Type) -> Type:
        display_name = flow_name or cls.__name__

        # -- wrap each node's _run / _run_async --

        def _wrap_node(node: Any) -> None:
            if getattr(node, "__pocketflow_observe_trace_wrapped__", False):
                return
            node.__pocketflow_observe_trace_wrapped__ = True

            if hasattr(node, "_run_async"):
                original = node._run_async

                async def wrapped(shared: Dict[str, Any]) -> Any:
                    return await _run_node_with_trace_async(
                        node, shared, original,
                        node_types, node_hook,
                        capture_input, capture_output, max_payload_len,
                    )

                node._run_async = wrapped
            else:
                original = node._run

                def wrapped(shared: Dict[str, Any]) -> Any:
                    return _run_node_with_trace(
                        node, shared, original,
                        node_types, node_hook,
                        capture_input, capture_output, max_payload_len,
                    )

                node._run = wrapped

        def _walk_and_wrap(start_node: Any) -> None:
            seen = set()
            stack = [start_node]
            while stack:
                n = stack.pop()
                if n is None or id(n) in seen:
                    continue
                seen.add(id(n))
                _wrap_node(n)
                for succ in getattr(n, "successors", {}).values():
                    stack.append(succ)

        # -- wrap the flow-level run / run_async to create the root span --

        if hasattr(cls, "run_async"):
            original_flow_run_async = cls.run_async

            async def new_run_async(self: Any, shared: Dict[str, Any]) -> Any:
                _walk_and_wrap(self.start_node)
                if not _ENABLED:
                    return await original_flow_run_async(self, shared)
                return await _run_flow_traced_async(
                    self, shared, original_flow_run_async,
                    display_name, flow_type,
                    session_id, user_id, tags, release, version, metadata,
                    capture_input, capture_output, max_payload_len,
                )

            cls.run_async = new_run_async  # type: ignore[method-assign]

        if hasattr(cls, "run"):
            original_flow_run = cls.run

            def new_run(self: Any, shared: Dict[str, Any]) -> Any:
                _walk_and_wrap(self.start_node)
                if not _ENABLED:
                    return original_flow_run(self, shared)
                return _run_flow_traced(
                    self, shared, original_flow_run,
                    display_name, flow_type,
                    session_id, user_id, tags, release, version, metadata,
                    capture_input, capture_output, max_payload_len,
                )

            cls.run = new_run  # type: ignore[method-assign]

        return cls

    return decorator


# ---------------------------------------------------------------------------
# Flow-level tracing (sync + async)
# ---------------------------------------------------------------------------

def _build_trace_attrs(
    shared: Dict[str, Any],
    session_id: Resolvable,
    user_id: Resolvable,
    tags: Resolvable,
    release: Resolvable,
    version: Resolvable,
    metadata: Resolvable,
) -> Dict[str, Any]:
    """Resolve all trace-level attrs against current shared."""
    attrs: Dict[str, Any] = {}
    for key, val in [
        ("session_id", session_id),
        ("user_id", user_id),
        ("tags", tags),
        ("release", release),
        ("version", version),
        ("metadata", metadata),
    ]:
        resolved = resolve(val, shared)
        if resolved is not None:
            attrs[key] = resolved
    return attrs


def _run_flow_traced(
    self: Any, shared: Dict[str, Any], original_run: Callable,
    display_name: str, flow_type: str,
    session_id: Resolvable, user_id: Resolvable, tags: Resolvable,
    release: Resolvable, version: Resolvable, metadata: Resolvable,
    capture_input: bool, capture_output: bool, max_payload_len: int,
) -> Any:
    from langfuse import propagate_attributes  # type: ignore

    trace_attrs = _build_trace_attrs(
        shared, session_id, user_id, tags, release, version, metadata
    )

    # propagate_attributes applies user_id/session_id/tags to every nested
    # observation automatically — we don't need to thread them through.
    propagate_kwargs = {k: v for k, v in trace_attrs.items() if k in {
        "session_id", "user_id", "tags", "release", "version", "metadata"
    }}

    start_kwargs: Dict[str, Any] = {"as_type": flow_type, "name": display_name}
    if capture_input:
        start_kwargs["input"] = _bounded(shared, max_payload_len)

    with (
        propagate_attributes(**propagate_kwargs),
        _langfuse.start_as_current_observation(**start_kwargs) as span,
    ):
        t0 = time.perf_counter()
        try:
            result = original_run(self, shared)
            elapsed = time.perf_counter() - t0
            update: Dict[str, Any] = {"metadata": {"elapsed_s": round(elapsed, 4)}}
            if capture_output:
                update["output"] = _bounded(shared, max_payload_len)
            span.update(**update)
            return result
        except Exception as e:
            span.update(level="ERROR", status_message=f"{type(e).__name__}: {e}")
            raise


async def _run_flow_traced_async(
    self: Any, shared: Dict[str, Any], original_run: Callable,
    display_name: str, flow_type: str,
    session_id: Resolvable, user_id: Resolvable, tags: Resolvable,
    release: Resolvable, version: Resolvable, metadata: Resolvable,
    capture_input: bool, capture_output: bool, max_payload_len: int,
) -> Any:
    from langfuse import propagate_attributes  # type: ignore

    trace_attrs = _build_trace_attrs(
        shared, session_id, user_id, tags, release, version, metadata
    )
    propagate_kwargs = {k: v for k, v in trace_attrs.items() if k in {
        "session_id", "user_id", "tags", "release", "version", "metadata"
    }}

    start_kwargs: Dict[str, Any] = {"as_type": flow_type, "name": display_name}
    if capture_input:
        start_kwargs["input"] = _bounded(shared, max_payload_len)

    with (
        propagate_attributes(**propagate_kwargs),
        _langfuse.start_as_current_observation(**start_kwargs) as span,
    ):
        t0 = time.perf_counter()
        try:
            result = await original_run(self, shared)
            elapsed = time.perf_counter() - t0
            update: Dict[str, Any] = {"metadata": {"elapsed_s": round(elapsed, 4)}}
            if capture_output:
                update["output"] = _bounded(shared, max_payload_len)
            span.update(**update)
            return result
        except Exception as e:
            span.update(level="ERROR", status_message=f"{type(e).__name__}: {e}")
            raise


# ---------------------------------------------------------------------------
# Per-node tracing (sync + async)
# ---------------------------------------------------------------------------

def _run_node_with_trace(
    node: Any, shared: Dict[str, Any], original: Callable,
    node_types: Optional[Dict[str, str]],
    node_hook: Optional[NodeHook],
    capture_input: bool, capture_output: bool, max_payload_len: int,
) -> Any:
    if not _ENABLED:
        return original(shared)

    name = type(node).__name__
    obs_type = node_observation_type(node, node_types)

    start_kwargs: Dict[str, Any] = {"as_type": obs_type, "name": f"node.{name}"}
    if capture_input:
        start_kwargs["input"] = _bounded(shared, max_payload_len)

    with _langfuse.start_as_current_observation(**start_kwargs) as span:
        t0 = time.perf_counter()
        try:
            action = original(shared)
            elapsed = time.perf_counter() - t0

            # Build default update payload
            update: Dict[str, Any] = {
                "metadata": {
                    "action": action or "default",
                    "elapsed_s": round(elapsed, 4),
                }
            }
            if capture_output:
                update["output"] = _bounded(shared, max_payload_len)

            # User hook — may override / extend anything
            if node_hook is not None:
                try:
                    extra = node_hook(node, shared, action, elapsed) or {}
                    _merge_update(update, extra)
                except Exception as e:  # never let hook crash the run
                    update["metadata"]["hook_error"] = f"{type(e).__name__}: {e}"

            span.update(**update)
            return action
        except Exception as e:
            span.update(level="ERROR", status_message=f"{type(e).__name__}: {e}")
            raise


async def _run_node_with_trace_async(
    node: Any, shared: Dict[str, Any], original: Callable,
    node_types: Optional[Dict[str, str]],
    node_hook: Optional[NodeHook],
    capture_input: bool, capture_output: bool, max_payload_len: int,
) -> Any:
    if not _ENABLED:
        return await original(shared)

    name = type(node).__name__
    obs_type = node_observation_type(node, node_types)

    start_kwargs: Dict[str, Any] = {"as_type": obs_type, "name": f"node.{name}"}
    if capture_input:
        start_kwargs["input"] = _bounded(shared, max_payload_len)

    with _langfuse.start_as_current_observation(**start_kwargs) as span:
        t0 = time.perf_counter()
        try:
            action = await original(shared)
            elapsed = time.perf_counter() - t0
            update: Dict[str, Any] = {
                "metadata": {
                    "action": action or "default",
                    "elapsed_s": round(elapsed, 4),
                }
            }
            if capture_output:
                update["output"] = _bounded(shared, max_payload_len)
            if node_hook is not None:
                try:
                    extra = node_hook(node, shared, action, elapsed) or {}
                    _merge_update(update, extra)
                except Exception as e:
                    update["metadata"]["hook_error"] = f"{type(e).__name__}: {e}"
            span.update(**update)
            return action
        except Exception as e:
            span.update(level="ERROR", status_message=f"{type(e).__name__}: {e}")
            raise


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _merge_update(base: Dict[str, Any], extra: Dict[str, Any]) -> None:
    """Merge a hook-returned dict into the span.update() kwargs.
    `metadata` is deep-merged; everything else is overwritten.
    """
    for k, v in extra.items():
        if k == "metadata" and isinstance(v, dict):
            base.setdefault("metadata", {}).update(v)
        else:
            base[k] = v


def _bounded(obj: Any, max_len: int) -> Any:
    """Return a bounded-size repr-ish form of obj, safe for serialization."""
    s = safe_repr(obj, max_len)
    return s


# ---------------------------------------------------------------------------
# trace_node — observe a single node with Langfuse, without needing a Flow
# ---------------------------------------------------------------------------

def trace_node(
    target: Any = None,
    *,
    observation_type: Optional[str] = None,
    node_hook: Optional[NodeHook] = None,
    capture_input: bool = True,
    capture_output: bool = True,
    max_payload_len: int = 2000,
) -> Any:
    """Add Langfuse tracing to a single node, class or instance.

    Like `@trace_flow`, but for one node run in isolation. A single node
    run becomes a root trace in Langfuse (no enclosing flow span).

    Three call shapes:

        # 1. Zero-arg class decorator
        @trace_node
        class MyNode(Node): ...

        # 2. With args
        @trace_node(observation_type="tool")
        class CallAPI(Node): ...

        # 3. On an instance
        node = trace_node(SomeNode(), observation_type="retriever")
        node.run(shared)

    If `observation_type` is given, it wins over any class attribute.
    Otherwise the node's `observation_type` attribute (or "span") is used.
    """

    def _apply(obj: Any) -> Any:
        if isinstance(obj, type):
            return _wrap_tracing_class(
                obj, observation_type, node_hook,
                capture_input, capture_output, max_payload_len,
            )
        _wrap_tracing_instance(
            obj, observation_type, node_hook,
            capture_input, capture_output, max_payload_len,
        )
        return obj

    if target is not None and (
        isinstance(target, type) or _looks_like_node_instance(target)
    ):
        return _apply(target)

    def decorator(obj: Any) -> Any:
        return _apply(obj)

    return decorator


def _looks_like_node_instance(obj: Any) -> bool:
    return (
        not isinstance(obj, type)
        and (hasattr(obj, "_run") or hasattr(obj, "_run_async"))
    )


def _wrap_tracing_class(
    cls: Type,
    obs_type_override: Optional[str],
    node_hook: Optional[NodeHook],
    capture_input: bool, capture_output: bool, max_payload_len: int,
) -> Type:
    # If an explicit type was given, stash it as a class attribute so the
    # normal resolver (_core.node_observation_type) picks it up.
    if obs_type_override is not None:
        cls.observation_type = obs_type_override  # type: ignore[attr-defined]

    if hasattr(cls, "_run_async"):
        original = cls._run_async  # type: ignore[attr-defined]

        async def wrapped(self: Any, shared: Dict[str, Any]) -> Any:
            bound = lambda s: original(self, s)  # noqa: E731
            return await _run_node_with_trace_async(
                self, shared, bound, None, node_hook,
                capture_input, capture_output, max_payload_len,
            )

        cls._run_async = wrapped  # type: ignore[attr-defined]

    if hasattr(cls, "_run"):
        original_sync = cls._run  # type: ignore[attr-defined]

        def wrapped_sync(self: Any, shared: Dict[str, Any]) -> Any:
            bound = lambda s: original_sync(self, s)  # noqa: E731
            return _run_node_with_trace(
                self, shared, bound, None, node_hook,
                capture_input, capture_output, max_payload_len,
            )

        cls._run = wrapped_sync  # type: ignore[attr-defined]

    return cls


def _wrap_tracing_instance(
    node: Any,
    obs_type_override: Optional[str],
    node_hook: Optional[NodeHook],
    capture_input: bool, capture_output: bool, max_payload_len: int,
) -> None:
    if getattr(node, "__pocketflow_observe_trace_wrapped__", False):
        return
    node.__pocketflow_observe_trace_wrapped__ = True

    if obs_type_override is not None:
        node.observation_type = obs_type_override

    if hasattr(node, "_run_async"):
        original = node._run_async

        async def wrapped(shared: Dict[str, Any]) -> Any:
            return await _run_node_with_trace_async(
                node, shared, original, None, node_hook,
                capture_input, capture_output, max_payload_len,
            )

        node._run_async = wrapped

    if hasattr(node, "_run"):
        original_sync = node._run

        def wrapped_sync(shared: Dict[str, Any]) -> Any:
            return _run_node_with_trace(
                node, shared, original_sync, None, node_hook,
                capture_input, capture_output, max_payload_len,
            )

        node._run = wrapped_sync
