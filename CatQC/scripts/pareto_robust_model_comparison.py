"""Retired entry point. Use rank_models.py with explicit ranking settings."""
import sys
if __name__ == "__main__":
    print("This release replaced Pareto analysis. Use rank_models.py; confirm reliability threshold and weights first.", file=sys.stderr)
    raise SystemExit(2)
