#!/usr/bin/env python3
"""Compatibility shim for source checkouts: same as the ``hyperdata`` command.

    python3 run_dashboard.py            # interactive menu
    python3 run_dashboard.py heatmap    # one dashboard directly
"""
from hyperdata_terminal.cli import main

if __name__ == "__main__":
    main()
