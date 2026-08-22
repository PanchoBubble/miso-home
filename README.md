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
