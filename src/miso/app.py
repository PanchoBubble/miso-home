"""Miso service entry point."""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from collections.abc import Sequence

from miso.config import ConfigError, Settings
from miso.http import create_server
from miso.memory import MemoryStore


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Miso local assistant runtime")
    result.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration and runtime paths, then exit",
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        settings.validate_runtime_paths()
        store = MemoryStore(settings.database_path)
        store.migrate()
        if store.integrity_check() != "ok":
            raise ConfigError("Miso database failed its integrity check")
    except ConfigError as error:
        print(f"configuration error: {error}")
        return 2

    if arguments.check_config:
        print("Miso configuration is valid")
        return 0

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    server = create_server(settings)

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logging.getLogger("miso").info("listening on %s:%s", settings.host, settings.port)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
    return 0
