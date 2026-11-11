"""Per-invocation execution diagnostics shared without mutable global state."""
from contextvars import ContextVar


current_execution: ContextVar[dict | None] = ContextVar("current_execution", default=None)


def record_error(stage: str, error: BaseException) -> None:
    execution = current_execution.get()
    if execution is not None:
        execution["errors"].append({"stage": stage, "type": type(error).__name__})