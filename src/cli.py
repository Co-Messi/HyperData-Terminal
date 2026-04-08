"""CLI entry point — allows `hyperdata` command after pip install."""
import os
import sys

# Ensure project root is on path (same as run_dashboard.py)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "src"))

def main():
    from run_dashboard import main as _main
    _main()
