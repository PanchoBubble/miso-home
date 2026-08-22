"""Translate provider-neutral function definitions to provider wire formats."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from miso.providers.base import ProviderProtocolError


def _function(tool: Mapping[str, object]) -> tuple[str, str, Mapping[str, object]]:
    name = tool.get("name")
    description = tool.get("description")
    schema = tool.get("input_schema")
    if not isinstance(name, str) or not isinstance(description, str):
        raise ProviderProtocolError("invalid provider-neutral tool definition")
    if not isinstance(schema, Mapping):
        raise ProviderProtocolError("tool input_schema must be an object")
    return name, description, schema


def ollama_tools(
    tools: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    result = []
    for tool in tools:
        name, description, schema = _function(tool)
        result.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": dict(schema),
                },
            }
        )
    return result


def openai_tools(
    tools: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    result = []
    for tool in tools:
        name, description, schema = _function(tool)
        result.append(
            {
                "type": "function",
                "name": name,
                "description": description,
                "parameters": dict(schema),
                "strict": True,
            }
        )
    return result
