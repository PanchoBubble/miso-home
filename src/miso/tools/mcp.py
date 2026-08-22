"""Allowlisted adapter boundary for approved MCP servers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from miso.tools.base import ToolContext, ToolDefinition


class MCPToolClient(Protocol):
    """Transport-neutral subset required from an MCP client implementation."""

    def call_tool(
        self,
        server: str,
        tool: str,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> Mapping[str, object]: ...


class MCPToolAdapter:
    """Turns explicitly approved remote tools into local tool definitions."""

    def __init__(self, client: MCPToolClient, approved_servers: frozenset[str]) -> None:
        self.client = client
        self.approved_servers = approved_servers

    def definition(
        self,
        *,
        local_name: str,
        server: str,
        remote_name: str,
        description: str,
        input_schema: Mapping[str, object],
        timeout_seconds: float = 15.0,
        redact_fields: frozenset[str] = frozenset(),
    ) -> ToolDefinition:
        if server not in self.approved_servers:
            raise ValueError(f"MCP server is not approved: {server}")
        if not remote_name.strip():
            raise ValueError("remote MCP tool name must not be empty")

        def call(arguments: Mapping[str, object], context: ToolContext) -> Mapping[str, object]:
            context.raise_if_cancelled()
            result = self.client.call_tool(server, remote_name, arguments, context)
            context.raise_if_cancelled()
            if not isinstance(result, Mapping):
                raise TypeError("MCP tool result must be an object")
            return dict(result)

        return ToolDefinition(
            name=local_name,
            description=description,
            input_schema=input_schema,
            handler=call,
            timeout_seconds=timeout_seconds,
            redact_fields=redact_fields,
        )
