#!/usr/bin/env python3
"""Evaluate declared assertions without creating questions implicitly."""

from _computation_cli import main

if __name__ == "__main__":
    raise SystemExit(main(operation="verify"))
