# pocketflow-observe

An observability wrapper for [PocketFlow](https://github.com/The-Pocket/PocketFlow),
the minimalist LLM framework (100 lines of code) that lets you build agents,
task pipelines, and RAG workflows with plain Python nodes and flows.

PocketFlow is intentionally minimal — it provides the graph execution engine
and nothing else. This package adds the observability layer that PocketFlow
doesn't ship: **console logging** and **Langfuse tracing**, applied as
decorators so your node code stays untouched.

- **`@log_flow`** / **`@log_node`** — Rich-powered console output (falls back to stdlib `logging`).
- **`@trace_flow`** / **`@trace_node`** — Langfuse tracing with observation types, session/user/tags, token usage, cost, and per-node hooks.
- **Pure add-on.** Nodes stay vanilla `pocketflow.Node` — no imports from this package needed in node code.
- **Composable.** Stack decorators in any order.
- **Soft dependencies.** Works without Rich or Langfuse installed — decorators degrade gracefully.

---

## Installation

```bash
pip install pocketflow-observe

# or with uv
uv add pocketflow-observe
```

### Extras

```bash
pip install "pocketflow-observe[logging]"   # + Rich
pip install "pocketflow-observe[tracing]"   # + Langfuse
pip install "pocketflow-observe[full]"      # both
```

The base install only requires `pocketflow`.

---

## Quick start

```python
from pocketflow import Flow, Node
from pocketflow_observe import log_flow, trace_flow, trace_llm, flush

class GetQuery(Node):
    def exec(self, _):
        return "What is 7 times 6?"
    def post(self, shared, prep_res, exec_res):
        shared["q"] = exec_res

class SearchDB(Node):
    observation_type = "retriever"        # pocketflow ignores it, pocketflow-observe reads it
    def prep(self, shared):
        return shared["q"]
    def exec(self, q):
        return ["7 * 6 = 42"]
    def post(self, shared, prep_res, exec_res):
        shared["docs"] = exec_res

class CallLLM(Node):
    observation_type = "generation"
    def prep(self, shared):
        return {"q": shared["q"], "docs": shared["docs"]}
    def exec(self, ctx):
        answer = "42"
        trace_llm(
            name="answer-llm",
            model="gpt-4o",
            input=ctx,
            output=answer,
            usage_details={"input": 120, "output": 2},
        )
        return answer
    def post(self, shared, prep_res, exec_res):
        shared["answer"] = exec_res

@trace_flow(session_id=lambda s: s.get("conv_id"), user_id="u_1", tags=["demo"])
@log_flow()
class QAFlow(Flow): pass

q, s, a = GetQuery(), SearchDB(), CallLLM()
q >> s >> a

QAFlow(start=q).run({"conv_id": "c_42"})
flush()
```

---

## `@log_flow` / `@log_node` — console logging

Wraps every node execution with INFO-level start/end markers and optional
DEBUG-level phase output (prep/exec/post return values).

```python
@log_flow(
    flow_name="DataPipeline",   # override displayed name (default: class name)
    show_types=True,            # show (retriever)/(tool)/... badges
    capture_phases=True,        # log phase return values at DEBUG level
)
class MyFlow(Flow): pass
```

For a single node without a flow:

```python
from pocketflow_observe import log_node

# As a class decorator
@log_node
class FetchData(Node):
    def exec(self, _): return fetch()

# With options
@log_node(capture_phases=False)
class QuietNode(Node):
    def exec(self, _): return "x"

# On a single instance at runtime
node = log_node(SomeNode())
node.run(shared)
```

Uses Rich when installed, falls back to stdlib `logging` otherwise.
Control verbosity with `POCKETFLOW_OBSERVE_LOG_LEVEL=DEBUG|INFO|WARNING`.

---

## `@trace_flow` / `@trace_node` — Langfuse tracing

Opt-in via environment variables:

```bash
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_HOST=https://cloud.langfuse.com   # optional, defaults to cloud
```

Without these, the decorator is a **safe no-op** — the flow runs normally.

```python
@trace_flow(
    flow_name="ResearchAgent",
    flow_type="chain",                                  # Langfuse observation type for the flow span (default: "chain")
    session_id=lambda shared: shared["conversation_id"],
    user_id=lambda shared: shared["user"]["id"],
    tags=["prod", "v2"],
    release="2025-q2",
    version="0.1.0",
    metadata={"region": "eu"},
    node_types={"SearchDB": "retriever"},               # override per-node observation types by class name
    node_hook=my_hook,                                   # called after each node (see below)
    capture_input=True,                                  # send shared as span input
    capture_output=True,                                 # send shared as span output
    max_payload_len=2000,                                # truncate payloads beyond this length
)
class MyFlow(Flow): pass
```

All resolvable arguments (`session_id`, `user_id`, `tags`, `release`, `version`,
`metadata`) accept a literal or a callable taking `shared`.
These use Langfuse's `propagate_attributes`, so every nested observation inherits
them — the whole trace is filterable by user/session/tag in the Langfuse UI.

### Node observation types

Three ways, resolved in priority order:

1. **`node_types` dict in the decorator** — `{"ClassName": "tool"}`
2. **Class attribute on the node** — `observation_type = "retriever"` (no import needed)
3. **Default** — `"span"`

Valid types: `span`, `agent`, `tool`, `retriever`, `generation`, `embedding`,
`evaluator`, `guardrail`, `chain`, `event`.

### Per-node hook

Runs after each node. Return a dict merged into the node's span:

```python
def my_hook(node, shared, action, elapsed):
    return {
        "metadata": {"attempt": getattr(node, "cur_retry", 0) + 1},
        "usage_details": shared.get("last_usage"),
    }
```

The return dict can include any `span.update()` field: `input`, `output`,
`metadata`, `usage_details`, `cost_details`, `model`, `model_parameters`,
`level`, `status_message`.

### `trace_node` — single node tracing

Same idea as `@log_node`, but for Langfuse. A standalone run becomes its own
root trace (no enclosing flow span).

```python
from pocketflow_observe import trace_node

@trace_node(observation_type="tool")
class CallAPI(Node):
    def exec(self, query): return api.get(query)

# Or on an instance
node = trace_node(SomeNode(), observation_type="retriever")
node.run(shared)
```

Takes the same `node_hook`, `capture_input`, `capture_output`, and
`max_payload_len` options as `@trace_flow`.

### Privacy controls

```python
@trace_flow(
    capture_input=False,     # don't send shared as span input
    capture_output=False,    # don't send as span output
    max_payload_len=500,     # truncate instead of full payload
)
```

---

## `trace_llm()` — nested LLM generation spans

Record an LLM call inside a node's `exec` as a nested Langfuse generation:

```python
from pocketflow_observe import trace_llm

class Answer(Node):
    def exec(self, ctx):
        answer = call_llm(ctx)
        trace_llm(
            name="answer-llm",
            model="gpt-4o",
            input=ctx,
            output=answer,
            as_type="generation",                                       # default
            usage_details={"input": 120, "output": 45},
            cost_details={"input": 0.0018, "output": 0.000375},         # optional
            model_parameters={"temperature": 0.0},                      # optional
            metadata={"source": "primary"},                             # optional
        )
        return answer
```

Safe no-op when tracing is disabled.

---

## Helpers

| Function | Purpose |
|---|---|
| `flush()` | Flush pending Langfuse spans. Call at program end or in short-lived processes. |
| `is_enabled()` | Returns `True` if Langfuse tracing is active (keys set and library installed). |
| `get_logger(name)` | Returns the internal `LoggerWrapper` — Rich-aware logger with node/flow lifecycle methods. |

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `POCKETFLOW_OBSERVE_LOG_LEVEL` | `INFO` | Console log verbosity |
| `POCKETFLOW_OBSERVE_ENABLED` | `1` | Set `0` to force-disable Langfuse even when keys are present |
| `LANGFUSE_PUBLIC_KEY` | — | Enables tracing when set |
| `LANGFUSE_SECRET_KEY` | — | Enables tracing when set |
| `LANGFUSE_HOST` | `cloud.langfuse.com` | Self-hosted Langfuse URL |

---

## Development

```bash
git clone https://github.com/sevesogit/pocketflow-observe.git
cd pocketflow-observe
uv sync
uv run pytest
```

## License

MIT — see `LICENSE`.
