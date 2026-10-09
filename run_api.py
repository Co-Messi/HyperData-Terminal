#!/usr/bin/env python3
"""Compatibility shim for source checkouts: same as ``hyperdata api``.

    python3 run_api.py              # default port 8420
    python3 run_api.py --port 8420  # explicit port
"""
import sys

from hyperdata_terminal.cli import main

if __name__ == "__main__":
    main(["api", *sys.argv[1:]])
