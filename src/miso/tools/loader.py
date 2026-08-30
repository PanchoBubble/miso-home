"""Hot-loadable tool modules discovered under a tools.d directory.

A module is a plain Python file exposing ``tool_definitions()``, which returns
the ToolDefinition objects it owns. Refreshing scans the directory, executes
the modules whose bytes changed, validates every definition, and swaps the
whole set of directory-owned tools into the registry in one commit.

Failure is contained per module: an invalid module is rejected with a visible
error, every other module keeps working, and a module whose replacement fails
validation keeps the version that is already registered.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import re
import sys
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from miso.identity import Actor, SYSTEM_ACTOR
from miso.tools.audit import AuditSink, audit_event
from miso.tools.base import ToolContext, ToolDefinition, ToolRegistry


ENTRY_POINT = "tool_definitions"
MODULE_NAMESPACE = "miso_tools_d"
MAX_MODULE_BYTES = 512 * 1024
MAX_MODULE_TOOLS = 16
REFRESH_TOOL_NAME = "tools_refresh"
LOGGER = logging.getLogger("miso.tools.loader")

MODULE_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class ToolModuleError(ValueError):
    """Raised when a tools.d module cannot be loaded or is not valid."""


@dataclass(frozen=True, slots=True)
class ToolModuleFailure:
    module: str
    path: str
    error: str

    def as_dict(self) -> dict[str, object]:
        return {"module": self.module, "path": self.path, "error": self.error}


@dataclass(frozen=True, slots=True)
class ToolRefreshReport:
    """What one refresh changed, and which modules were rejected."""

    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    failed: tuple[ToolModuleFailure, ...] = ()
    modules: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    @property
    def summary(self) -> str:
        parts: list[str] = []
        for label, names in (
            ("added", self.added),
            ("updated", self.updated),
            ("removed", self.removed),
        ):
            if names:
                parts.append(f"{label} {', '.join(names)}")
        headline = "; ".join(parts) if parts else "no tool changes"
        if self.failed:
            rejected = ", ".join(failure.module for failure in self.failed)
            return f"Tools refreshed: {headline}. Rejected {rejected}."
        return f"Tools refreshed: {headline}."

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "added": list(self.added),
            "updated": list(self.updated),
            "removed": list(self.removed),
            "unchanged": list(self.unchanged),
            "failed": [failure.as_dict() for failure in self.failed],
            "modules": list(self.modules),
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class _LoadedModule:
    name: str
    path: Path
    digest: str
    definitions: tuple[ToolDefinition, ...]

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(definition.name for definition in self.definitions)


class ToolDirectoryLoader:
    """Scan, validate, and hot-register the tool modules under one directory."""

    def __init__(
        self,
        registry: ToolRegistry,
        directory: Path,
        *,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self.registry = registry
        self.directory = Path(directory)
        self.audit_sink = audit_sink
        self._lock = threading.Lock()
        self._modules: dict[str, _LoadedModule] = {}
        self._last_report: ToolRefreshReport | None = None

    def refresh(
        self, *, module: str | None = None, actor: Actor = SYSTEM_ACTOR
    ) -> ToolRefreshReport:
        """Reload the directory, or a single module when ``module`` is given."""
        with self._lock:
            report = self._refresh_locked(module)
            self._last_report = report
        self._record(report, module, actor)
        return report

    def status(self) -> dict[str, object]:
        report = self._last_report
        return {
            "directory": str(self.directory),
            "modules": sorted(self._modules),
            "tools": sorted(
                name for state in self._modules.values() for name in state.tool_names
            ),
            "last_refresh": report.as_dict() if report is not None else None,
        }

    def tool_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=REFRESH_TOOL_NAME,
            description=(
                "Reload household tool modules from the tools directory without "
                "restarting the service. Optionally refresh a single module."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "module": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "pattern": MODULE_NAME_PATTERN,
                        "description": "File stem of a single module to refresh",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=self._handle_refresh,
            timeout_seconds=30.0,
        )

    def _handle_refresh(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> Mapping[str, object]:
        module = arguments.get("module")
        return self.refresh(
            module=str(module) if isinstance(module, str) else None,
            actor=context.actor,
        ).as_dict()

    def _refresh_locked(self, module: str | None) -> ToolRefreshReport:
        failures: list[ToolModuleFailure] = []
        try:
            discovered = self._discover()
        except OSError as error:
            return ToolRefreshReport(
                unchanged=self._registered_names(),
                failed=(
                    ToolModuleFailure(
                        module or "*",
                        str(self.directory),
                        f"tools directory is unreadable: {error}",
                    ),
                ),
                modules=tuple(sorted(self._modules)),
            )

        if module is not None:
            targets = {module}
            if module not in discovered and module not in self._modules:
                failures.append(
                    ToolModuleFailure(
                        module,
                        str(self.directory / f"{module}.py"),
                        "module was not found in the tools directory",
                    )
                )
        else:
            targets = set(discovered) | set(self._modules)

        reloaded: set[str] = set()
        desired: dict[str, _LoadedModule] = {
            name: state for name, state in self._modules.items() if name not in targets
        }
        for name in sorted(targets):
            path = discovered.get(name)
            previous = self._modules.get(name)
            if path is None:
                continue
            try:
                digest = _digest(path)
            except OSError as error:
                failures.append(
                    ToolModuleFailure(name, str(path), f"module is unreadable: {error}")
                )
                if previous is not None:
                    desired[name] = previous
                continue
            if previous is not None and previous.digest == digest:
                desired[name] = previous
                continue
            try:
                definitions = _load_module(name, path)
            except Exception as error:
                failures.append(
                    ToolModuleFailure(name, str(path), _describe_error(error))
                )
                if previous is not None:
                    desired[name] = previous
                continue
            reloaded.add(name)
            desired[name] = _LoadedModule(name, path, digest, definitions)

        resolved, conflicts = self._resolve_conflicts(desired)
        failures.extend(conflicts)
        reloaded &= set(resolved)

        before = {
            name: state.name
            for state in self._modules.values()
            for name in state.tool_names
        }
        after = {
            name: state.name for state in resolved.values() for name in state.tool_names
        }
        try:
            self.registry.apply_sources(
                {name: state.definitions for name, state in resolved.items()}
            )
        except ValueError as error:
            return ToolRefreshReport(
                unchanged=self._registered_names(),
                failed=tuple(failures)
                + (
                    ToolModuleFailure(
                        module or "*", str(self.directory), _describe_error(error)
                    ),
                ),
                modules=tuple(sorted(self._modules)),
            )
        self._modules = resolved

        added = tuple(sorted(set(after) - set(before)))
        removed = tuple(sorted(set(before) - set(after)))
        kept = set(after) & set(before)
        updated = tuple(sorted(name for name in kept if after[name] in reloaded))
        unchanged = tuple(sorted(kept - set(updated)))
        report = ToolRefreshReport(
            added=added,
            updated=updated,
            removed=removed,
            unchanged=unchanged,
            failed=tuple(failures),
            modules=tuple(sorted(resolved)),
        )
        return report

    def _resolve_conflicts(
        self, desired: Mapping[str, _LoadedModule]
    ) -> tuple[dict[str, _LoadedModule], list[ToolModuleFailure]]:
        """Give each tool name to one module, rejecting later claimants."""
        reserved = {
            name: "the service itself" for name in self.registry.static_names()
        }
        resolved: dict[str, _LoadedModule] = {}
        failures: list[ToolModuleFailure] = []
        for name in sorted(desired):
            state = desired[name]
            clash = next(
                (tool for tool in state.tool_names if tool in reserved), None
            )
            if clash is not None:
                failures.append(
                    ToolModuleFailure(
                        name,
                        str(state.path),
                        f"tool {clash} is already registered by {reserved[clash]}",
                    )
                )
                continue
            for tool in state.tool_names:
                reserved[tool] = f"module {name}"
            resolved[name] = state
        return resolved, failures

    def _registered_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(name for state in self._modules.values() for name in state.tool_names)
        )

    def _discover(self) -> dict[str, Path]:
        if not self.directory.is_dir():
            raise OSError(f"{self.directory} is not a directory")
        found: dict[str, Path] = {}
        for path in sorted(self.directory.glob("*.py")):
            name = path.stem
            if name.startswith("_") or not _is_module_name(name):
                continue
            if not path.is_file():
                continue
            found[name] = path
        return found

    def _record(
        self, report: ToolRefreshReport, module: str | None, actor: Actor
    ) -> None:
        for failure in report.failed:
            LOGGER.error(
                "tool module rejected: %s (%s): %s",
                failure.module,
                failure.path,
                failure.error,
            )
        if report.changed:
            LOGGER.info("tool refresh: %s", report.summary)
        if self.audit_sink is None:
            return
        self.audit_sink.record(
            audit_event(
                "tool_refresh",
                directory=str(self.directory),
                module=module,
                added=list(report.added),
                updated=list(report.updated),
                removed=list(report.removed),
                failed=[failure.as_dict() for failure in report.failed],
                actor=actor.actor_id,
                actor_source=actor.source,
            )
        )


def _is_module_name(name: str) -> bool:
    return re.fullmatch(MODULE_NAME_PATTERN, name) is not None


def _digest(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > MAX_MODULE_BYTES:
        raise OSError(f"module is larger than {MAX_MODULE_BYTES} bytes")
    return hashlib.sha256(data).hexdigest()


def _describe_error(error: BaseException) -> str:
    if isinstance(error, ToolModuleError):
        return str(error)
    return f"{type(error).__name__}: {error}"


def _load_module(name: str, path: Path) -> tuple[ToolDefinition, ...]:
    """Execute one module in its own namespace and validate what it exports.

    The source is compiled here rather than imported through the file loader:
    a module rewritten in the same second at the same length hits stale cached
    bytecode, which would silently keep serving the previous handler.
    """
    module_name = f"{MODULE_NAMESPACE}.{name}"
    code = compile(path.read_bytes(), str(path), "exec")
    spec = importlib.util.spec_from_loader(module_name, loader=None, origin=str(path))
    if spec is None:
        raise ToolModuleError("module could not be imported")
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(path)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        exec(code, module.__dict__)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    entry = getattr(module, ENTRY_POINT, None)
    if not callable(entry):
        raise ToolModuleError(f"module must define {ENTRY_POINT}()")
    definitions = entry()
    return _validated(definitions)


def _validated(definitions: object) -> tuple[ToolDefinition, ...]:
    if isinstance(definitions, ToolDefinition):
        definitions = (definitions,)
    if isinstance(definitions, (str, bytes, Mapping)) or not isinstance(
        definitions, (Sequence, Iterable)
    ):
        raise ToolModuleError(f"{ENTRY_POINT}() must return tool definitions")
    collected = tuple(definitions)
    if not collected:
        raise ToolModuleError(f"{ENTRY_POINT}() returned no tool definitions")
    if len(collected) > MAX_MODULE_TOOLS:
        raise ToolModuleError(
            f"{ENTRY_POINT}() returned more than {MAX_MODULE_TOOLS} tool definitions"
        )
    seen: set[str] = set()
    for definition in collected:
        try:
            ToolRegistry.validate_definition(definition)
        except ValueError as error:
            raise ToolModuleError(str(error)) from error
        if definition.name in seen:
            raise ToolModuleError(f"module defines {definition.name} twice")
        seen.add(definition.name)
    return collected
