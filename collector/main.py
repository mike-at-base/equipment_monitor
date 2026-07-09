"""
Collector entry point.
    python collector/main.py [--config path/to/config.yaml] [--dry-run]

--dry-run  resolves all OPC UA nodes and prints them, then exits without
           writing any data.  Useful for verifying node paths before going live.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.schema import init_schema
from collector.opc_client import build_clients_from_config, siemens_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
# Keep third-party OPC stack noise out of container logs. The collector's own
# INFO logs remain enabled, but asyncua callback payloads are extremely verbose.
logging.getLogger("asyncua").setLevel(logging.WARNING)
logging.getLogger("asyncua.common.subscription").setLevel(logging.WARNING)
log = logging.getLogger("collector")


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


async def run_all(clients) -> None:
    await asyncio.gather(*[c.run() for c in clients])


def main() -> None:
    parser = argparse.ArgumentParser(description="Equipment Monitor Collector")
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config.yaml (default: config.yaml)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Resolve OPC UA nodes and print them, then exit"
    )
    args = parser.parse_args()

    config_path = args.config
    if not os.path.isabs(config_path):
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), config_path)

    log.info("Loading config from %s", config_path)
    config = load_config(config_path)

    log.info("Initialising database schema")
    init_schema()

    clients = build_clients_from_config(config)
    if not clients:
        log.error("No enabled PLCs found in config — exiting")
        sys.exit(1)

    total_ems = sum(len(c._trackers) for c in clients)
    log.info("Built %d OPC UA client(s) covering %d equipment module(s)",
             len(clients), total_ems)

    if args.dry_run:
        log.info("--dry-run: would subscribe to nodes for %d EMs across %d PLC(s)",
                 total_ems, len(clients))
        for c in clients:
            log.info("  PLC: %s  endpoint: %s  EMs: %d",
                     c.plc_name, c.endpoint, len(c._trackers))
            for em_id, t in c._trackers.items():
                quoted = siemens_path(t.em_db_path)
                log.info("    em_id=%d  %s/%s", em_id, t.station, t.em_label)
                log.info("      OPC path: ns=3;s=%s.status.mode.automatic (example)",
                         quoted)
        return

    log.info("Starting collector — press Ctrl+C to stop")
    try:
        asyncio.run(run_all(clients))
    except KeyboardInterrupt:
        log.info("Collector stopped")


if __name__ == "__main__":
    main()
