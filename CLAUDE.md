# CLAUDE.md — context for AI coding assistants

## What is this?

`pocketflow-observe` is a pure add-on for [PocketFlow](https://github.com/The-Pocket/PocketFlow) (minimalist LLM framework, ~100 lines). It adds **console logging** (`@log_flow` / `@log_node`) and **Langfuse tracing** (`@trace_flow` / `@trace_node`) via decorators — node code stays untouched.

## Project layout

```
src/pocketflow_observe/
  __init__.py      # public API — re-exports everything
  _core.py         # shared helpers: resolve(), node_observation_type(), safe_repr()
  _logger.py       # Rich-aware logger wrapper (falls back to stdlib logging)
  logging.py       # @log_flow, @log_node decorators
  tracing.py       # @trace_flow, @trace_node, trace_llm(), flush(), is_enabled()
tests/
  test_decorators.py
examples/
  qa.py            # full flow example (query → search → LLM)
  single_node.py   # standalone node example
```

## Key design decisions

- **No subclassing.** Decorators wrap vanilla `pocketflow.Flow` / `pocketflow.Node` — we monkey-patch `_run` / `_run_async` at flow-start time, not at import time.
- **Soft dependencies.** `rich` and `langfuse` are optional extras. Decorators degrade gracefully (no-op or stdlib fallback) when they're missing.
- **Resolvable pattern.** Decorator args like `session_id`, `user_id`, `tags` accept a literal OR a callable taking `shared`. See `_core.resolve()`.
- **Node observation types** resolve in priority order: decorator `node_types` dict > node class attribute `observation_type` > default `"span"`.

## Build & test

```bash
uv sync              # install deps
uv run pytest -v     # run tests
uv run ruff check src tests  # lint
```

## Versioning

- Single source of truth: `__version__` in `src/pocketflow_observe/__init__.py`. `pyproject.toml` reads it dynamically via hatchling.
- Merging a version bump to `main` auto-creates a git tag via `.github/workflows/auto-tag.yml`.
- The tag triggers the publish job in `.github/workflows/ci.yml`.

## Rules

- Target Python 3.9+. No walrus operators, no `type` aliases, no `X | Y` union syntax.
- Use `from __future__ import annotations` in every module.
- Never import `pocketflow_observe` inside node/flow user code — the decorators are applied externally.
- Don't add `rich` or `langfuse` as hard dependencies — they must stay in optional extras.
- Ruff config: line-length 100, select E/F/I/B/SIM. Run `uv run ruff check` before committing.
