#!/usr/bin/env python3
"""HyperData headless API server — no terminal dashboard, just the API.

Usage:
    python3 run_api.py              # default port 8420
    python3 run_api.py --port 8420  # explicit port
"""
import asyncio
import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from src.data_layer.hub import HyperDataHub


def main():
    parser = argparse.ArgumentParser(description="HyperData headless API server")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()

    log_dir = Path(_ROOT) / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "hyperdata.log", maxBytes=5 * 1024 * 1024, backupCount=3,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG, handlers=[fh])

    async def run():
        hub = HyperDataHub(demo=args.demo, api_port=args.port)
        await hub.start()
        logging.getLogger(__name__).info("HyperData API running on port %d (headless)", args.port)
        try:
            while True:
                await asyncio.sleep(60)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await hub.stop()

    asyncio.run(run())


if __name__ == "__main__":
    main()
