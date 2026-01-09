"""Context variable for sharing callback handler across agents.

This allows sub-agents to access the parent's callback_handler for tool tracking.
"""

import contextvars
from typing import Any, Callable

# Context variable to store the current callback handler
_callback_handler_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    'callback_handler', default=None
)

# Global fallback (for when context doesn't propagate)
_global_callback_handler: Any = None


def set_callback_handler(handler: Any) -> None:
    """Set the current callback handler.

    Args:
        handler: The callback handler to use for tool tracking.
    """
    global _global_callback_handler
    _global_callback_handler = handler
    _callback_handler_var.set(handler)


def get_callback_handler() -> Any:
    """Get the current callback handler.

    Returns:
        The current callback handler, or None if not set.
    """
    handler = _callback_handler_var.get(None)
    if handler is None:
        handler = _global_callback_handler
    return handler


def clear_callback_handler() -> None:
    """Clear the callback handler."""
    global _global_callback_handler
    _global_callback_handler = None
    _callback_handler_var.set(None)
