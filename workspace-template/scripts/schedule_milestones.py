#!/usr/bin/env python3
"""Report explicit-clock cadence states without dispatching implicitly."""

from _computation_cli import main

if __name__ == "__main__":
    raise SystemExit(main(operation="schedule"))
