"""Customer-scoped tools: trusted context, strict schemas, safe results."""

from app.tools.context import CustomerContext
from app.tools.registry import (
    TOOL_SPECS,
    ToolSpec,
    execute_tool,
    llm_tool_definitions,
    validate_arguments,
)
from app.tools.results import ToolResult

__all__ = [
    "CustomerContext",
    "TOOL_SPECS",
    "ToolResult",
    "ToolSpec",
    "execute_tool",
    "llm_tool_definitions",
    "validate_arguments",
]
