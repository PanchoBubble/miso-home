"""Typed boundary for locally allowlisted assistant tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

ToolHandler = Callable[[Mapping[str, object]], Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, object]
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if not definition.name or not definition.name.replace("_", "").isalnum():
            raise ValueError("tool name must contain only letters, numbers, and underscores")
        if definition.name in self._tools:
            raise ValueError(f"tool is already registered: {definition.name}")
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as error:
            raise KeyError(f"tool is not allowlisted: {name}") from error

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))
