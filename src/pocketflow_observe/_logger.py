"""pocketflow_observe.logger — Rich-powered pretty logger.

Works without Rich installed (falls back to stdlib logging).
Can be turned up/down via POCKETFLOW_OBSERVE_LOG_LEVEL.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

try:
    from rich.console import Console
    from rich.logging import RichHandler
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

_console = Console(stderr=True) if _HAS_RICH else None
_configured = False


def _configure_once() -> None:
    global _configured
    if _configured:
        return
    level = os.getenv("POCKETFLOW_OBSERVE_LOG_LEVEL", "INFO").upper()
    if _HAS_RICH:
        handler = RichHandler(
            console=_console,
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            markup=True,
        )
        logging.basicConfig(level=level, format="%(message)s", handlers=[handler])
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        )
    _configured = True


def _fmt(val: Any, max_len: int = 160) -> str:
    try:
        s = repr(val)
    except Exception:
        return f"<unreprable {type(val).__name__}>"
    s = s.replace("\n", " ")
    if len(s) > max_len:
        s = s[:max_len] + f"… (+{len(s) - max_len})"
    return s


class LoggerWrapper:
    def __init__(self, name: str) -> None:
        _configure_once()
        self._log = logging.getLogger(name)

    # pass-through
    def debug(self, m: str, *a: Any, **kw: Any) -> None: self._log.debug(m, *a, **kw)
    def info(self, m: str, *a: Any, **kw: Any) -> None: self._log.info(m, *a, **kw)
    def warning(self, m: str, *a: Any, **kw: Any) -> None: self._log.warning(m, *a, **kw)
    def error(self, m: str, *a: Any, **kw: Any) -> None: self._log.error(m, *a, **kw)

    # node lifecycle
    def node_start(self, name: str, obs_type: str = "span") -> None:
        badge = f"[dim]({obs_type})[/]" if obs_type != "span" else ""
        self._log.info(f"[bold cyan]▶ node[/] [bold]{name}[/] {badge}")

    def node_phase(self, name: str, phase: str, value: Any) -> None:
        self._log.debug(f"  [dim]{name}.{phase}[/] → {_fmt(value)}")

    def node_end(self, name: str, action: Optional[str], elapsed: float) -> None:
        act = action or "default"
        self._log.info(
            f"[green]✓ node[/] [bold]{name}[/] "
            f"[dim]action=[/]{act} [dim]({elapsed * 1000:.1f}ms)[/]"
        )

    def node_error(self, name: str, exc: Exception) -> None:
        self._log.error(f"[red]✗ node[/] [bold]{name}[/] {type(exc).__name__}: {exc}")

    # flow lifecycle
    def flow_start(self, name: str) -> None:
        self._log.info(f"[bold magenta]╭─ flow[/] [bold]{name}[/]")

    def flow_end(self, name: str, elapsed: float) -> None:
        self._log.info(
            f"[bold magenta]╰─ flow[/] [bold]{name}[/] [dim]done ({elapsed * 1000:.1f}ms)[/]"
        )

    def flow_error(self, name: str, exc: Exception) -> None:
        self._log.error(
            f"[red]╰─ flow[/] [bold]{name}[/] failed: {type(exc).__name__}: {exc}"
        )


def get_logger(name: str = "pocketflow_observe") -> LoggerWrapper:
    return LoggerWrapper(name)
