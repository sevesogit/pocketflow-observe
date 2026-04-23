"""
Single-node observation — no Flow needed.

Useful for unit-testing a node in isolation, or for scripts that call
one node at a time.
"""

from pocketflow import Node

from pocketflow_observe import log_node, trace_node, flush


# --- 1. Class decorator, zero args -----------------------------------------

@log_node
class FetchData(Node):
    def exec(self, _):
        return {"rows": [1, 2, 3]}

    def post(self, shared, prep_res, exec_res):
        shared["data"] = exec_res


# --- 2. Class decorator with args ------------------------------------------

@trace_node(observation_type="tool")
@log_node(capture_phases=False)  # quieter — skip DEBUG phase lines
class CallWeatherAPI(Node):
    def prep(self, shared):
        return shared["city"]

    def exec(self, city):
        # pretend we hit an API
        return {"city": city, "temp_c": 18}

    def post(self, shared, prep_res, exec_res):
        shared["weather"] = exec_res


# --- 3. Wrap a single instance at runtime ----------------------------------

class DoSomething(Node):
    def exec(self, _):
        return "done"

    def post(self, shared, prep_res, exec_res):
        shared["ok"] = True


if __name__ == "__main__":
    shared = {"city": "Paris"}

    # Run the decorated classes — every instance logs
    FetchData().run(shared)
    CallWeatherAPI().run(shared)

    # Wrap a single instance ad-hoc — doesn't affect other instances of DoSomething
    watched = log_node(DoSomething())
    watched.run(shared)

    print("\nfinal shared:", shared)
    flush()
