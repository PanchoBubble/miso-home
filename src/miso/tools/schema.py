"""Small dependency-free JSON Schema subset used by Miso tool inputs."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence


class SchemaError(ValueError):
    """Raised when a tool schema or input does not satisfy the strict contract."""


_SUPPORTED = {
    "$schema", "type", "properties", "required", "additionalProperties",
    "items", "minItems", "maxItems", "uniqueItems", "minLength",
    "maxLength", "pattern", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "enum", "const", "description",
}
_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def validate_tool_schema(schema: Mapping[str, object]) -> None:
    if not isinstance(schema, Mapping):
        raise SchemaError("tool input schema must be an object")
    if schema.get("type") != "object":
        raise SchemaError("tool input schema root type must be object")
    if schema.get("additionalProperties") is not False:
        raise SchemaError("tool input schema must set additionalProperties to false")
    _validate_schema(schema, "$")


def _validate_schema(schema: Mapping[str, object], path: str) -> None:
    unsupported = set(schema) - _SUPPORTED
    if unsupported:
        raise SchemaError(f"{path}: unsupported schema keyword {sorted(unsupported)[0]}")
    schema_type = schema.get("type")
    if schema_type not in _TYPES:
        raise SchemaError(f"{path}: type must be one of {sorted(_TYPES)}")
    properties = schema.get("properties", {})
    if schema_type == "object":
        if not isinstance(properties, Mapping):
            raise SchemaError(f"{path}.properties must be an object")
        for name, child in properties.items():
            if not isinstance(name, str) or not isinstance(child, Mapping):
                raise SchemaError(f"{path}.properties must contain schema objects")
            _validate_schema(child, f"{path}.properties.{name}")
        required = schema.get("required", [])
        if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
            raise SchemaError(f"{path}.required must be an array")
        if len(set(required)) != len(required) or any(
            not isinstance(name, str) or name not in properties for name in required
        ):
            raise SchemaError(f"{path}.required must contain unique property names")
        additional = schema.get("additionalProperties", True)
        if not isinstance(additional, bool) and not isinstance(additional, Mapping):
            raise SchemaError(f"{path}.additionalProperties must be boolean or a schema")
        if isinstance(additional, Mapping):
            _validate_schema(additional, f"{path}.additionalProperties")
    if schema_type == "array":
        items = schema.get("items")
        if not isinstance(items, Mapping):
            raise SchemaError(f"{path}.items must be a schema")
        _validate_schema(items, f"{path}.items")
    for keyword in ("minItems", "maxItems", "minLength", "maxLength"):
        if keyword in schema and (
            not isinstance(schema[keyword], int)
            or isinstance(schema[keyword], bool)
            or schema[keyword] < 0
        ):
            raise SchemaError(f"{path}.{keyword} must be a non-negative integer")
    if "pattern" in schema:
        try:
            re.compile(str(schema["pattern"]))
        except re.error as error:
            raise SchemaError(f"{path}.pattern is invalid: {error}") from error


def validate_instance(schema: Mapping[str, object], value: object) -> None:
    _validate_value(schema, value, "$")


def _validate_value(schema: Mapping[str, object], value: object, path: str) -> None:
    expected = schema["type"]
    if not _matches_type(str(expected), value):
        raise SchemaError(f"{path}: expected {expected}")
    if "const" in schema and value != schema["const"]:
        raise SchemaError(f"{path}: value does not match const")
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)):
            raise SchemaError(f"{path}: schema enum must be an array")
        if value not in choices:
            raise SchemaError(f"{path}: value is not in enum")
    if expected == "object":
        assert isinstance(value, Mapping)
        properties = schema.get("properties", {})
        assert isinstance(properties, Mapping)
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise SchemaError(f"{path}: missing required property {missing[0]}")
        for name, item in value.items():
            if not isinstance(name, str):
                raise SchemaError(f"{path}: property names must be strings")
            child = properties.get(name)
            if child is None:
                additional = schema.get("additionalProperties", True)
                if additional is False:
                    raise SchemaError(f"{path}: unexpected property {name}")
                if isinstance(additional, Mapping):
                    _validate_value(additional, item, f"{path}.{name}")
            else:
                assert isinstance(child, Mapping)
                _validate_value(child, item, f"{path}.{name}")
    elif expected == "array":
        assert isinstance(value, (list, tuple))
        if len(value) < int(schema.get("minItems", 0)):
            raise SchemaError(f"{path}: too few items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise SchemaError(f"{path}: too many items")
        if schema.get("uniqueItems") and len({repr(item) for item in value}) != len(value):
            raise SchemaError(f"{path}: items must be unique")
        items = schema["items"]
        assert isinstance(items, Mapping)
        for index, item in enumerate(value):
            _validate_value(items, item, f"{path}[{index}]")
    elif expected == "string":
        assert isinstance(value, str)
        if len(value) < int(schema.get("minLength", 0)):
            raise SchemaError(f"{path}: string is too short")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise SchemaError(f"{path}: string is too long")
        if "pattern" in schema and re.search(str(schema["pattern"]), value) is None:
            raise SchemaError(f"{path}: string does not match pattern")
    elif expected in {"integer", "number"}:
        numeric = float(value)
        if not math.isfinite(numeric):
            raise SchemaError(f"{path}: number must be finite")
        bounds = (
            ("minimum", lambda actual, limit: actual >= limit),
            ("maximum", lambda actual, limit: actual <= limit),
            ("exclusiveMinimum", lambda actual, limit: actual > limit),
            ("exclusiveMaximum", lambda actual, limit: actual < limit),
        )
        for keyword, predicate in bounds:
            if keyword in schema and not predicate(numeric, float(schema[keyword])):
                raise SchemaError(f"{path}: violates {keyword}")


def _matches_type(expected: str, value: object) -> bool:
    return {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, (list, tuple)),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }[expected]
