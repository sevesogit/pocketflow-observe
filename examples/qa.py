"""
Minimal example: decorators applied to a vanilla pocketflow.Flow.

Nodes import ONLY from pocketflow. The two decorators on `MyFlow` are the
only places pocketflow-observe appears. Remove them and this is pure pocketflow.

Run:
    uv run python examples/qa.py

Enable Langfuse tracing by exporting:
    LANGFUSE_PUBLIC_KEY=pk-lf-...
    LANGFUSE_SECRET_KEY=sk-lf-...
"""

from pocketflow import Flow, Node

from pocketflow_observe import flush, log_flow, trace_flow, trace_llm


# --- vanilla pocketflow nodes ------------------------------------------------

class GetQuery(Node):
    def exec(self, _):
        return "What is 7 times 6?"

    def post(self, shared, prep_res, exec_res):
        shared["q"] = exec_res


class SearchDB(Node):
    # Just a class attribute — pocketflow ignores it, pocketflow-observe reads it.
    observation_type = "retriever"

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
        # Nested generation span with tokens + cost, right inside this node
        trace_llm(
            name="answer-llm",
            model="claude-opus-4-7",
            input=ctx,
            output=answer,
            usage_details={"input": 120, "output": 2, "cache_read_input_tokens": 80},
            cost_details={"input": 0.0018, "output": 0.00015},
            model_parameters={"temperature": 0.0, "max_tokens": 32},
        )
        return answer

    def post(self, shared, prep_res, exec_res):
        shared["answer"] = exec_res


# --- the only pocketflow-observe touch point: two decorators on the Flow class --------

@trace_flow(
    flow_name="MathQA",
    session_id=lambda shared: shared.get("conversation_id", "default"),
    user_id="user_123",
    tags=["demo"],
    version="0.1.0",
)
@log_flow()
class MyFlow(Flow):
    pass


if __name__ == "__main__":
    q, s, a = GetQuery(), SearchDB(), CallLLM()
    q >> s >> a

    shared = {"conversation_id": "conv-42"}
    MyFlow(start=q).run(shared)
    print(f"\n→ answer: {shared['answer']}")
    flush()
