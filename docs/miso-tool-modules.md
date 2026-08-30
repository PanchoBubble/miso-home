# Hot tool modules (`tools.d`)

Miso loads extra tools from `/var/lib/miso/tools.d` at startup and reloads them
on demand, so a new household tool becomes invocable without restarting
`miso.service`. Built-in tools (household, calendar, weather, developer shell,
`tools_refresh` itself) are registered by the service and are never replaced or
removed by a refresh.

Override the directory with `MISO_TOOLS_DIR`; the path must be absolute. A
missing directory is reported as a refresh error and changes nothing.

## Module contract

A module is a single `*.py` file whose stem is lowercase
(`^[a-z][a-z0-9_]{0,63}$`); files starting with `_` and non-`.py` files are
ignored. The module defines `tool_definitions()` returning the `ToolDefinition`
objects it owns (at most 16 per module):

```python
from miso.tools import ToolDefinition


def porch_light(arguments, context):
    return {"summary": f"Porch light {arguments['state']}."}


def tool_definitions():
    return [
        ToolDefinition(
            name="porch_light",
            description="Switch the porch light on or off.",
            input_schema={
                "type": "object",
                "properties": {
                    "state": {"type": "string", "enum": ["on", "off"]},
                },
                "required": ["state"],
                "additionalProperties": False,
            },
            handler=porch_light,
            timeout_seconds=10.0,
        )
    ]
```

Handlers run under the same boundary as every other tool: arguments are
validated against the schema before the handler runs, the deadline and
cooperative cancellation arrive in `context`, and every invocation is audited.
Returning a `summary` string makes the result speakable.

A module may not claim a tool name that the service registered or that another
module already owns; the loser is rejected and its file is left in place.

## Refreshing

Three entry points, all of which run the same scan:

| Surface | Call |
| --- | --- |
| HTTP | `POST /api/tools/refresh` with `{}`, or `{"module": "porch"}` |
| Tool | `tools_refresh` with `{}` or `{"module": "porch"}` |
| Voice | "refresh tools" / "reload your tools" / "recarga las herramientas" |

`GET /api/tools` lists every registered tool with the module that owns it, plus
the last refresh report.

A refresh executes only the modules whose bytes changed, validates every
definition, and swaps the whole set of directory-owned tools into the registry
in one commit. Failure is contained per module:

- an invalid module is rejected and named in `failed[]`, logged at `ERROR`, and
  recorded in the tool audit as a `tool_refresh` event;
- every other module keeps working;
- a module whose replacement fails validation keeps the version that is already
  registered, so a half-written file never removes a working tool;
- an invocation that is already running keeps the definition it resolved, and
  the next invocation uses the new one.

Passing `module` validates and registers that one file and leaves every other
module untouched, which is the path a generated tool takes after its owner
approves it.

The report looks like this:

```json
{
  "ok": true,
  "added": ["porch_light"],
  "updated": [],
  "removed": [],
  "unchanged": [],
  "failed": [],
  "modules": ["porch"],
  "summary": "Tools refreshed: added porch_light."
}
```

## Trust boundary

A file in `tools.d` runs as the `miso` service user with no sandbox, exactly
like the rest of the runtime. The directory is `miso:miso` 0750: keep it that
way, and treat dropping a module there as the approval step. Generated modules
must not be written to `tools.d` until their owner has reviewed them.
