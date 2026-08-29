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
from miso.tools.household import (
    HouseholdStore,
    ScheduledItemWorker,
    register_household_tools,
)
from miso.tools.google_calendar import (
    AUTHORIZATION_SCOPES,
    CALENDAR_SCOPES,
    GoogleAuthorizationRequired,
    GoogleCalendarAdapter,
    GoogleCalendarConfig,
    GoogleCalendarError,
    GoogleOAuthClient,
    GoogleOAuthSession,
    GoogleToken,
    GoogleTokenStore,
    register_google_calendar_tools,
)
from miso.tools.mcp import MCPToolAdapter, MCPToolClient
from miso.tools.schema import SchemaError
from miso.tools.shell import DeveloperShellController
from miso.tools.weather import (
    OpenMeteoWeatherAdapter,
    WeatherConfig,
    register_weather_tools,
    weather_tool_definition,
)


def create_runtime_registry(
    state_dir: Path,
    database_path: Path | None = None,
    google_calendar_config: GoogleCalendarConfig | None = None,
    weather_config: WeatherConfig | None = None,
) -> ToolRegistry:
    """Create the production registry with a durable local audit sink."""
    registry = ToolRegistry(JsonlAuditLog(state_dir / "audit" / "tools.jsonl"))
    if database_path is not None:
        register_household_tools(registry, database_path)
    if google_calendar_config is not None:
        register_google_calendar_tools(registry, google_calendar_config)
    if weather_config is not None:
        register_weather_tools(registry, weather_config)
    return registry


__all__ = [
    "DeveloperShellController",
    "HouseholdStore",
    "AUTHORIZATION_SCOPES",
    "CALENDAR_SCOPES",
    "GoogleAuthorizationRequired",
    "GoogleCalendarAdapter",
    "GoogleCalendarConfig",
    "GoogleCalendarError",
    "GoogleOAuthClient",
    "GoogleOAuthSession",
    "GoogleToken",
    "GoogleTokenStore",
    "InMemoryAuditLog",
    "JsonlAuditLog",
    "MCPToolAdapter",
    "MCPToolClient",
    "OpenMeteoWeatherAdapter",
    "SchemaError",
    "ScheduledItemWorker",
    "ToolCancelled",
    "ToolContext",
    "ToolDeadlineExceeded",
    "ToolDefinition",
    "ToolRegistry",
    "ToolRejected",
    "ToolResult",
    "ToolStatus",
    "WeatherConfig",
    "create_runtime_registry",
    "register_household_tools",
    "register_google_calendar_tools",
    "register_weather_tools",
    "weather_tool_definition",
]
