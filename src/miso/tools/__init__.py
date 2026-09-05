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
from miso.tools.loader import (
    REFRESH_TOOL_NAME,
    ToolDirectoryLoader,
    ToolModuleError,
    ToolModuleFailure,
    ToolRefreshReport,
)
from miso.tools.mcp import MCPToolAdapter, MCPToolClient
from miso.tools.schema import SchemaError
from miso.tools.shell import DeveloperShellController
from miso.tools.weather import (
    WEATHER_LOCATION_SETTING,
    OpenMeteoWeatherAdapter,
    WeatherConfig,
    WeatherHome,
    WeatherPoller,
    WeatherSnapshot,
    WeatherSnapshotStore,
    create_weather_poller,
    register_weather_tools,
    weather_panel,
    weather_set_home_tool_definition,
    weather_status,
    weather_tool_definition,
)


def create_runtime_registry(
    state_dir: Path,
    database_path: Path | None = None,
    google_calendar_config: GoogleCalendarConfig | None = None,
    weather_config: WeatherConfig | None = None,
    weather_snapshots: WeatherSnapshotStore | None = None,
    weather_home: WeatherHome | None = None,
) -> ToolRegistry:
    """Create the production registry with a durable local audit sink."""
    registry = ToolRegistry(JsonlAuditLog(state_dir / "audit" / "tools.jsonl"))
    if database_path is not None:
        register_household_tools(registry, database_path)
    if google_calendar_config is not None:
        register_google_calendar_tools(registry, google_calendar_config)
    if weather_config is not None:
        register_weather_tools(
            registry,
            weather_config,
            snapshots=weather_snapshots,
            home=weather_home,
        )
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
    "REFRESH_TOOL_NAME",
    "OpenMeteoWeatherAdapter",
    "SchemaError",
    "ScheduledItemWorker",
    "ToolCancelled",
    "ToolContext",
    "ToolDeadlineExceeded",
    "ToolDefinition",
    "ToolDirectoryLoader",
    "ToolModuleError",
    "ToolModuleFailure",
    "ToolRefreshReport",
    "ToolRegistry",
    "ToolRejected",
    "ToolResult",
    "ToolStatus",
    "WEATHER_LOCATION_SETTING",
    "WeatherConfig",
    "WeatherHome",
    "WeatherPoller",
    "WeatherSnapshot",
    "WeatherSnapshotStore",
    "create_runtime_registry",
    "create_weather_poller",
    "register_household_tools",
    "register_google_calendar_tools",
    "register_weather_tools",
    "weather_panel",
    "weather_set_home_tool_definition",
    "weather_status",
    "weather_tool_definition",
]
