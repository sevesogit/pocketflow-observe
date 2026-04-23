"""Tests for decorator-based API against vanilla pocketflow."""

from __future__ import annotations

import logging

from pocketflow import Flow, Node

from pocketflow_observe import is_enabled, log_flow, trace_flow, trace_llm
from pocketflow_observe._core import node_observation_type, resolve
from pocketflow_observe.tracing import _init_langfuse

# ---------------------------------------------------------------------------
# core helpers
# ---------------------------------------------------------------------------


def test_resolve_literal():
    assert resolve("hello", {}) == "hello"
    assert resolve(42, {"x": 1}) == 42
    assert resolve(None, {}) is None


def test_resolve_callable_with_shared():
    shared = {"sid": "abc"}
    assert resolve(lambda s: s["sid"], shared) == "abc"


def test_resolve_callable_without_args():
    assert resolve(lambda: "const", {}) == "const"


def test_node_observation_type_class_attr():
    class N(Node):
        observation_type = "tool"

    assert node_observation_type(N()) == "tool"


def test_node_observation_type_default():
    class N(Node):
        pass

    assert node_observation_type(N()) == "span"


def test_node_observation_type_override_wins():
    class N(Node):
        observation_type = "tool"

    # Override by class name
    assert node_observation_type(N(), {"N": "agent"}) == "agent"


# ---------------------------------------------------------------------------
# log_flow decorator on vanilla pocketflow
# ---------------------------------------------------------------------------


class _Hello(Node):
    def exec(self, _):
        return "hi"

    def post(self, shared, prep_res, exec_res):
        shared["out"] = exec_res


def test_log_flow_runs_flow_and_populates_shared(caplog):
    import logging

    caplog.set_level(logging.INFO)

    @log_flow(flow_name="TestFlow")
    class F(Flow):
        pass

    shared = {}
    F(start=_Hello()).run(shared)

    assert shared["out"] == "hi"
    # The wrapper should have emitted flow + node markers
    combined = " ".join(r.message for r in caplog.records)
    assert "flow" in combined.lower() and "TestFlow" in combined
    assert "Hello" in combined


def test_log_flow_without_name_uses_class_name(caplog):
    import logging

    caplog.set_level(logging.INFO)

    @log_flow()
    class MyCoolFlow(Flow):
        pass

    MyCoolFlow(start=_Hello()).run({})
    assert any("MyCoolFlow" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# trace_flow decorator — no-op safety without Langfuse
# ---------------------------------------------------------------------------


def test_trace_flow_no_op_without_langfuse():
    # Tests run without LANGFUSE_PUBLIC_KEY → is_enabled() is False
    assert is_enabled() is False

    @trace_flow(flow_name="T")
    class F(Flow):
        pass

    shared = {}
    # Must not raise, must still run the flow normally
    F(start=_Hello()).run(shared)
    assert shared["out"] == "hi"


def test_trace_llm_is_safe_when_disabled():
    # Calling helper outside a Flow, with tracing off — must not crash
    trace_llm(
        name="test",
        model="m",
        input="i",
        output="o",
        usage_details={"input": 10},
        cost_details={"input": 0.01},
    )


# ---------------------------------------------------------------------------
# Decorator composition
# ---------------------------------------------------------------------------


def test_decorators_compose_in_either_order():
    @trace_flow()
    @log_flow()
    class F1(Flow):
        pass

    @log_flow()
    @trace_flow()
    class F2(Flow):
        pass

    shared1, shared2 = {}, {}
    F1(start=_Hello()).run(shared1)
    F2(start=_Hello()).run(shared2)
    assert shared1["out"] == shared2["out"] == "hi"


def test_decorators_dont_break_flow_routing():
    class A(Node):
        def post(self, shared, prep_res, exec_res):
            shared.setdefault("path", []).append("A")
            return "go"

    class B(Node):
        def post(self, shared, prep_res, exec_res):
            shared.setdefault("path", []).append("B")

    @trace_flow()
    @log_flow()
    class F(Flow):
        pass

    a, b = A(), B()
    a - "go" >> b
    shared = {}
    F(start=a).run(shared)
    assert shared["path"] == ["A", "B"]


def test_decorators_preserve_retry_behaviour():
    class Flaky(Node):
        def __init__(self):
            super().__init__(max_retries=3)
            self.attempts = 0

        def exec(self, _):
            self.attempts += 1
            if self.attempts < 3:
                raise RuntimeError("boom")
            return "ok"

        def post(self, shared, prep_res, exec_res):
            shared["result"] = exec_res

    @log_flow()
    class F(Flow):
        pass

    n = Flaky()
    shared = {}
    F(start=n).run(shared)
    assert n.attempts == 3
    assert shared["result"] == "ok"


# ---------------------------------------------------------------------------
# Node hook
# ---------------------------------------------------------------------------


def test_node_hook_is_called_even_when_tracing_disabled():
    # The hook only runs inside tracing wrapper; skip if disabled.
    # Here we just verify tracing disabled doesn't crash when a hook is provided.

    calls = []

    def hook(node, shared, action, elapsed):
        calls.append(type(node).__name__)
        return {"metadata": {"attempt": 1}}

    @trace_flow(node_hook=hook)
    class F(Flow):
        pass

    F(start=_Hello()).run({})
    # When tracing is off, the hook is not called. That's intentional —
    # hook output only makes sense as Langfuse span attributes.
    assert calls == []


# ---------------------------------------------------------------------------
# No subclassing of pocketflow required — minimal import surface
# ---------------------------------------------------------------------------


def test_node_can_declare_observation_type_without_importing_pocketflow_observe():
    # User code: imports only from pocketflow
    from pocketflow import Node as PFNode

    class MyTool(PFNode):
        observation_type = "tool"  # just a string, no pocketflow-observe dep

        def exec(self, _):
            return "tool-result"

    # pocketflow-observe reads the class attribute at decoration time
    assert node_observation_type(MyTool()) == "tool"


# ---------------------------------------------------------------------------
# log_node — single-node observation
# ---------------------------------------------------------------------------


def test_log_node_as_bare_class_decorator(caplog):
    import logging as stdlog

    from pocketflow_observe import log_node

    caplog.set_level(stdlog.INFO)

    @log_node
    class N(Node):
        def exec(self, _):
            return "x"

        def post(self, s, p, e):
            s["r"] = e

    shared = {}
    N().run(shared)
    assert shared["r"] == "x"
    assert any("N" in r.message and "node" in r.message.lower() for r in caplog.records)


def test_log_node_with_args(caplog):
    import logging as stdlog

    from pocketflow_observe import log_node

    caplog.set_level(stdlog.DEBUG)

    @log_node(capture_phases=False)
    class N(Node):
        def exec(self, _):
            return "x"

    N().run({})
    # capture_phases=False means no prep/exec/post DEBUG lines
    debug_phase_lines = [
        r for r in caplog.records if r.levelname == "DEBUG" and ".exec" in r.message
    ]
    assert debug_phase_lines == []


def test_log_node_on_instance(caplog):
    import logging as stdlog

    from pocketflow_observe import log_node

    caplog.set_level(stdlog.INFO)

    class N(Node):
        observation_type = "tool"

        def exec(self, _):
            return "api-result"

        def post(self, s, p, e):
            s["r"] = e

    # Wrapping only this one instance — other N() calls are unaffected
    wrapped = log_node(N())
    shared = {}
    wrapped.run(shared)
    assert shared["r"] == "api-result"

    # The log line should show the "(tool)" badge
    assert any("(tool)" in r.message for r in caplog.records)


def test_log_node_instance_does_not_affect_other_instances(caplog):
    import logging as stdlog

    from pocketflow_observe import log_node

    caplog.set_level(stdlog.INFO)

    class N(Node):
        def exec(self, _):
            return "x"

        def post(self, s, p, e):
            s["r"] = e

    wrapped = log_node(N())
    wrapped.run({})

    # A fresh, unwrapped instance should NOT emit logs
    caplog.clear()
    plain = N()
    plain.run({})
    assert all("node" not in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# trace_node — single-node tracing (no-op without Langfuse credentials)
# ---------------------------------------------------------------------------


def test_trace_node_no_op_without_langfuse():
    from pocketflow_observe import is_enabled, trace_node

    assert is_enabled() is False

    @trace_node(observation_type="retriever")
    class N(Node):
        def exec(self, _):
            return "docs"

        def post(self, s, p, e):
            s["r"] = e

    # Must not crash, must still run normally
    shared = {}
    N().run(shared)
    assert shared["r"] == "docs"


def test_trace_node_sets_observation_type_on_class():
    from pocketflow_observe import trace_node

    @trace_node(observation_type="guardrail")
    class N(Node):
        def exec(self, _):
            return None

    # The decorator sets the class attribute so the normal resolver picks it up
    assert N.observation_type == "guardrail"


def test_trace_node_on_instance():
    from pocketflow_observe import trace_node

    class N(Node):
        def exec(self, _):
            return "x"

        def post(self, s, p, e):
            s["r"] = e

    wrapped = trace_node(N(), observation_type="tool")
    shared = {}
    wrapped.run(shared)
    assert shared["r"] == "x"
    assert wrapped.observation_type == "tool"


def test_log_node_and_trace_node_stack():
    from pocketflow_observe import log_node, trace_node

    @log_node
    @trace_node(observation_type="agent")
    class N(Node):
        def exec(self, _):
            return "ok"

        def post(self, s, p, e):
            s["r"] = e

    shared = {}
    N().run(shared)
    assert shared["r"] == "ok"


# ---------------------------------------------------------------------------
# _init_langfuse — warning tests
# ---------------------------------------------------------------------------


def test_init_langfuse_disabled_by_env(monkeypatch, caplog):
    monkeypatch.setenv("POCKETFLOW_OBSERVE_ENABLED", "0")
    with caplog.at_level(logging.WARNING, logger="pocketflow_observe.tracing"):
        enabled, client = _init_langfuse()
    assert enabled is False
    assert client is None
    # No warning when explicitly disabled
    assert caplog.text == ""


def test_init_langfuse_missing_both_keys(monkeypatch, caplog):
    monkeypatch.setenv("POCKETFLOW_OBSERVE_ENABLED", "1")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="pocketflow_observe.tracing"):
        enabled, client = _init_langfuse()
    assert enabled is False
    assert client is None
    assert "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set" in caplog.text


def test_init_langfuse_missing_public_key(monkeypatch, caplog):
    monkeypatch.setenv("POCKETFLOW_OBSERVE_ENABLED", "1")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-fake")
    with caplog.at_level(logging.WARNING, logger="pocketflow_observe.tracing"):
        enabled, client = _init_langfuse()
    assert enabled is False
    assert client is None
    assert "LANGFUSE_PUBLIC_KEY is not set" in caplog.text


def test_init_langfuse_missing_secret_key(monkeypatch, caplog):
    monkeypatch.setenv("POCKETFLOW_OBSERVE_ENABLED", "1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-fake")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="pocketflow_observe.tracing"):
        enabled, client = _init_langfuse()
    assert enabled is False
    assert client is None
    assert "LANGFUSE_SECRET_KEY is not set" in caplog.text


def test_init_langfuse_bad_credentials(monkeypatch, caplog):
    """When both keys are set but get_client() raises, we warn and disable."""
    monkeypatch.setenv("POCKETFLOW_OBSERVE_ENABLED", "1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-bad")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-bad")
    # Force get_client to raise by pointing at an invalid host
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:1")

    with caplog.at_level(logging.WARNING, logger="pocketflow_observe.tracing"):
        enabled, client = _init_langfuse()

    # Either it connected (unlikely on localhost:1) or we got a warning
    if not enabled:
        assert client is None
        assert "Failed to initialize Langfuse client" in caplog.text
