"""Validated tool registry and guarded adapter boundaries."""

from pathlib import Path

from miso.tools.audit import InMemoryAuditLog, JsonlAuditLog
from miso.tools.base import (
    ToolCancelled,
    ToolContext,
    ToolDeadlineExceeded,
    ToolDefinition,
    ToolRegistry,
    ToolRejected,
    ToolResult,
    ToolStatus,
)
from miso.tools.mcp import MCPToolAdapter, MCPToolClient
from miso.tools.schema import SchemaError
from miso.tools.shell import DeveloperShellController


def create_runtime_registry(state_dir: Path) -> ToolRegistry:
    """Create the production registry with a durable local audit sink."""
    return ToolRegistry(JsonlAuditLog(state_dir / "audit" / "tools.jsonl"))

__all__ = [
    "DeveloperShellController",
    "InMemoryAuditLog",
    "JsonlAuditLog",
    "MCPToolAdapter",
    "MCPToolClient",
    "SchemaError",
    "ToolCancelled",
    "ToolContext",
    "ToolDeadlineExceeded",
    "ToolDefinition",
    "ToolRegistry",
    "ToolRejected",
    "ToolResult",
    "ToolStatus",
    "create_runtime_registry",
]
