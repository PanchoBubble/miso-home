# Miso

Miso is a local-first household assistant for the existing Pancho Raspberry Pi.
The first milestone is a provider-neutral text runtime with local SQLite memory,
validated tools, deterministic model routing, and a LAN dashboard. Voice and
hardware integration are later phases.

## Local development

Python 3.11 or newer is the only runtime dependency for the initial scaffold.

```bash
make test
make integration-test
make run
```

`make run` creates private development directories under `.local/` and listens
on `127.0.0.1:8090`. Check it with:

```bash
curl --fail http://127.0.0.1:8090/healthz
```

## Pi deployment

The Pi storage layout must first be installed with
`ops/configure-miso-storage.sh`. Copy the repository to the Pi, then run:

```bash
sudo ops/install-miso-runtime.sh
curl --fail http://127.0.0.1:8090/healthz
```

The installer places root-owned application code in `/opt/miso/app`, installs
`miso.service`, and runs the service as the unprivileged `miso` system user.
Optional environment overrides belong in `/etc/miso/miso.env`, never in Git.

The first Pi provider is Ollama on `127.0.0.1:11434`. Its systemd drop-in keeps
downloaded models under `/var/lib/miso/models/ollama`; the initial deployment
uses `qwen3:0.6b` as a small ARM64 smoke-test model. Larger-model benchmarking
and routing are tracked separately because the 0.6B model is useful for proving
streaming and tool-call mechanics, not for final assistant quality.

## Tool security boundary

All assistant tools are registered explicitly with a strict object-shaped JSON
Schema (`additionalProperties: false`). Requests are validated before handlers
run and return structured success, rejection, error, timeout, or cancellation
results. The service constructs its runtime registry with a private JSONL audit
log under `state_dir / "audit" / "tools.jsonl"`; arguments named in a tool's
`redact_fields` are removed from audit records.

MCP tools must be adapted through `MCPToolAdapter` with an explicit server
allowlist. The developer command tool is disabled by default, accepts argument
arrays rather than shell text, restricts execution to an approved directory and
executable allowlist, and can only be enabled for a bounded interval through its
dashboard-facing controller. Its status includes the visible scope and expiry.

## Household tools

The runtime registers durable timer, reminder, and shared shopping-list tools
against the same transactional SQLite database as memory. A background worker
normalizes scheduled timestamps to UTC, atomically fires overdue items (also on
process restart), and emits durable audit events. Every mutation increments a
revision. Shopping removals are retained as tombstones so operator views can
inspect history. Tool results use stable object shapes suitable for both local
models and the dashboard.

## Optional model providers

The Pi Ollama adapter remains the default local provider. A separate LAN Ollama
tier can be configured with `MISO_LAN_OLLAMA_URL` and
`MISO_LAN_OLLAMA_MODEL`. Hosted GPT uses the OpenAI Responses streaming API and
is disabled unless `MISO_OPENAI_API_KEY` is present; its model can be selected
with `MISO_OPENAI_MODEL`. Put these values in root-owned `/etc/miso/miso.env`
(recommended mode `0600`), never in source control. Hosted requests set
`store: false`, translate strict tool schemas to function definitions, and do
not include credentials in request bodies or settings representations.
